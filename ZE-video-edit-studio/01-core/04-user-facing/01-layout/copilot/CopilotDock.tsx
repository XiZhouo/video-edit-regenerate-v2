/**
 * CopilotDock — 常驻 AI 协作对话面板(框架级,每个 ZE web 工具自动获得)。
 *
 * 【单一助手(2026-07-19 定稿)】整个页面只有一个 AI 助手实体 = 精灵变形体
 * (GuideLayer/spriteEngine)。本组件**不再渲染自己的小球**,只贡献对话面板:
 * 点击精灵(热点经 assistantBus 的 toggle_chat)开合面板;面板「教我用」发 open_tours
 * 交精灵播教程;busy 态经总线让精灵呼吸加速。用户发一句 → 后端一轮 `claude -p` /
 * `codex exec` agentic 操作本工具(经 loopback /mcp,source="ai"),工具的实体改动经
 * 既有 /api/events SSE 实时渲染;助手一句简洁回话回气泡。
 *
 * 聊天气泡是**临时私有**的会话态(本组件 useState,不入 Document、不持久、刷新即清);只有 AI
 * 造成的实体改动才持久(那是真 Operation)。切项目 → 新对话上下文。
 *
 * 分层:JSX 只发 className + data-*(视觉全在 03-css-rules/02-atoms/shell/copilot-shell);
 * 拖动/定位用 data-side 属性驱动 CSS,无内联视觉样式。
 * 预留(本期不实现):语音输入、图片附件、独立展开/收纳按钮、自由拖拽定位。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { authHeader } from '@ze/core/04-user-facing/03-authentication'
import { apiGetAiOptions, apiUndoTurn } from '@ze/core/04-user-facing/03-authentication/aiOptions'
import { notifyCredentialsChanged } from '@ze/core/04-user-facing/03-authentication/aiRoutes'
import { ZeIcon } from '../lib/zeIcon'
import { CopilotAccountDialog } from './CopilotAccountDialog'
import { publishAssistant, subscribeAssistant } from './assistantBus'
import { summarizeUndoResult, type ChangeCardSummary } from './changeCardModel'
import { buildConfirmCardView, type ConfirmCardPayload } from './confirmCardModel'
import { AssistantStage } from './AssistantStage'
import type { TurnState } from './stageModel'

const API_BASE: string = ((import.meta as { env?: { VITE_API_BASE?: string } }).env?.VITE_API_BASE) ?? ''
const SIDE_KEY = 'ze.copilot.side'

type Role = 'user' | 'assistant' | 'system'
type Msg = { id: string; role: Role; text: string; images?: string[] }
type Attachment = { id: string; name: string; dataUri: string }

const MAX_IMAGES = 6

export type CopilotDockProps = {
  /** 当前打开的项目 id;无项目时挂件不显示(没有可操作的对象)。 */
  projectId?: string
  /** Required CAS fence for the project namespace this conversation may mutate. */
  expectedGeneration?: number
  /** 当前跨编辑器选中(entity_type → active id);自动作为上下文带给 AI。 */
  selection?: Record<string, string | null>
  /** 由 config `ze.ai.copilot_enabled` 控制;关则不挂。 */
  enabled?: boolean
}

let _seq = 0
const nextId = (): string => `m${_seq++}`

export function isExpectedCopilotGeneration(incoming: unknown, expected: number): boolean {
  const generation = Number(incoming)
  return Number.isSafeInteger(generation) && generation > 0 && generation === expected
}

export function CopilotDock({
  projectId, expectedGeneration, selection, enabled = true,
}: CopilotDockProps) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [side, setSide] = useState<'left' | 'right'>(
    () => (localStorage.getItem(SIDE_KEY) === 'left' ? 'left' : 'right'))
  const [messages, setMessages] = useState<Msg[]>([])
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [busy, setBusy] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [hasAccount, setHasAccount] = useState<boolean | null>(null)
  const [acctOpen, setAcctOpen] = useState(false)
  // §7.3/§7.4 P0-C 变更卡:最近一轮的 change_set 摘要 + turn_id(供「撤销本轮」);新一轮开始即清。
  const [lastTurn, setLastTurn] = useState<{ turnId: string; cs: ChangeCardSummary } | null>(null)
  const [undoing, setUndoing] = useState(false)
  // §7.3/§5.6 W1-5:待确认载荷(经 assistantBus 从精灵侧转来;null=当前无待确认)。
  const [pending, setPending] = useState<Record<string, unknown> | null>(null)
  // §7.3 W1-4:本轮当前阶段(经 assistantBus 从 /api/events 的 assistant_stage 帧转来)。
  const [liveStage, setLiveStage] = useState<{ stage: string; tool: string } | null>(null)
  const requestEpochRef = useRef(0)
  const requestAbortRef = useRef<AbortController | null>(null)

  // ephemeral:切项目 → 新对话上下文(旧气泡清空;实体改动已各自持久)。
  useEffect(() => {
    requestEpochRef.current += 1
    requestAbortRef.current?.abort()
    requestAbortRef.current = null
    setMessages([])
    setAttachments([])
    setBusy(false)
    return () => {
      requestEpochRef.current += 1
      requestAbortRef.current?.abort()
      requestAbortRef.current = null
    }
  }, [expectedGeneration, projectId])

  // 单一助手协调:精灵点击(toggle_chat)开合面板;面板开合/停靠边/忙碌态回播给精灵。
  useEffect(() => subscribeAssistant((frame) => {
    if (frame.type === 'toggle_chat') setOpen((o) => !o)
    // W1-5:精灵接到的待确认转到面板渲染成整张卡(0721 §5.6:说清做什么/影响/为什么)。
    // 面板开着时才有意义;关着由精灵气泡提示,点开就看到卡。
    else if (frame.type === 'pending_confirm') setPending(frame.payload)
    // W1-4:阶段帧驱动过程卡。done 稍留一下再清 —— 立刻清等于「完成」这一段没人看得见。
    else if (frame.type === 'stage') {
      setLiveStage({ stage: frame.stage, tool: frame.tool })
      if (frame.stage === 'done') window.setTimeout(() => setLiveStage(null), 1200)
    }
  }), [])
  useEffect(() => { publishAssistant({ type: 'chat_state', open, side }) }, [open, side])
  useEffect(() => { publishAssistant({ type: 'busy', busy }) }, [busy])

  // 打开面板 / 关掉账号弹窗后 → 经 ProviderHub resolver 查 agent.tool-use 能力有没有可用连接
  // (§7.2 P0-B 预检:没绑就引导,不让人白输一遍才报错)。走统一 /api/ai/options(capability=agent),
  // 覆盖 Codex/Claude 官方登录 + reauth/能力不匹配等态,比自查凭据 provider 名更收口。
  useEffect(() => {
    if (!open) return
    let cancelled = false
    apiGetAiOptions({ featureId: 'assistant.chat', capability: 'agent', require: ['agent.tool_use'] })
      .then((snap) => { if (!cancelled) setHasAccount(snap.has_available) })
      .catch(() => { if (!cancelled) setHasAccount(true) })   // 查不了别挡路
    return () => { cancelled = true }
  }, [open, acctOpen])

  const addFiles = (files: FileList | null) => {
    const imgs = Array.from(files ?? []).filter((f) => f.type.startsWith('image/'))
    imgs.forEach((f) => {
      const reader = new FileReader()
      reader.onload = () => setAttachments((a) =>
        a.length >= MAX_IMAGES ? a : [...a, { id: nextId(), name: f.name, dataUri: String(reader.result ?? '') }])
      reader.readAsDataURL(f)
    })
    if (fileRef.current) fileRef.current.value = ''   // allow re-picking the same file
  }

  // 新气泡/思考态 → 滚到底看最新(真实滚动条,历史可上翻)。
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, busy, open])

  const flipSide = () => {
    setSide((s) => {
      const next = s === 'right' ? 'left' : 'right'
      localStorage.setItem(SIDE_KEY, next)
      return next
    })
  }

  const selected = selection
    ? Object.entries(selection).filter(([, id]) => !!id).map(([type, id]) => ({ type, id: String(id) }))
    : []

  const send = useCallback(async () => {
    const text = draft.trim()
    const images = attachments.map((a) => a.dataUri)
    if ((!text && images.length === 0) || busy || !projectId
        || !Number.isSafeInteger(expectedGeneration) || (expectedGeneration ?? 0) <= 0) return
    const requestGeneration = Number(expectedGeneration)
    const requestEpoch = ++requestEpochRef.current
    const controller = new AbortController()
    requestAbortRef.current?.abort()
    requestAbortRef.current = controller
    setDraft(''); setAttachments([])
    setLastTurn(null)                     // 新一轮开始:旧变更卡失效(撤销只针对最近一轮)
    setMessages((m) => [...m, { id: nextId(), role: 'user', text, images: images.length ? images : undefined }])
    setBusy(true)
    try {
      const r = await fetch(
        `${API_BASE}/api/ai/chat?project_id=${encodeURIComponent(projectId)}`,
        {
          method: 'POST',
          credentials: 'include',
          signal: controller.signal,
          headers: { 'Content-Type': 'application/json', ...authHeader() },
          body: JSON.stringify({
            text,
            selection: selected,
            images,
            expected_generation: requestGeneration,
          }),
        },
      )
      const data = await r.json().catch(() => ({}))
      if (!r.ok) {
        const msg = (data && (data.error?.message || data.message)) || t('copilot.error')
        throw new Error(String(msg))
      }
      // The AI process may outlive a cross-tab reset before this tab receives
      // its SSE fence.  Never render an answer produced for the old namespace.
      if (!isExpectedCopilotGeneration(data?.project_generation, requestGeneration)) return
      if (requestEpochRef.current === requestEpoch) {
        setMessages((m) => [...m, { id: nextId(), role: 'assistant', text: String(data.reply ?? '') }])
        // §7.3 本轮有实体改动 → 记下 change_set 摘要 + turn_id,渲染变更卡供整轮撤销。
        if (data?.turn_id && data?.change_set && (data.change_set.count ?? 0) > 0) {
          setLastTurn({ turnId: String(data.turn_id), cs: data.change_set as ChangeCardSummary })
        }
      }
    } catch (e) {
      if (requestEpochRef.current === requestEpoch && (e as Error).name !== 'AbortError') {
        setMessages((m) => [...m, { id: nextId(), role: 'system', text: (e as Error).message || t('copilot.error') }])
      }
    } finally {
      if (requestEpochRef.current === requestEpoch) {
        requestAbortRef.current = null
        setBusy(false)
      }
    }
  }, [draft, attachments, busy, projectId, expectedGeneration, selected, t])

  // §7.4 整轮撤销:对最近一轮 change_set 逐个 dispatch 补偿命令(后端 Form B),回报撤销/保留数。
  const undoLastTurn = useCallback(async () => {
    if (!lastTurn || undoing) return
    setUndoing(true)
    try {
      const res = await apiUndoTurn(lastTurn.turnId)
      const { undone, kept } = summarizeUndoResult(res)
      const lines: string[] = []
      if (undone > 0) lines.push(t('copilot.undoneCount', { count: undone }))
      if (kept > 0) lines.push(t('copilot.undoKept', { count: kept }))
      if (lines.length === 0) lines.push(t('copilot.undoNothing'))
      setMessages((m) => [...m, ...lines.map((text) => ({ id: nextId(), role: 'system' as Role, text }))])
      setLastTurn(null)
    } catch {
      setMessages((m) => [...m, { id: nextId(), role: 'system', text: t('copilot.error') }])
    } finally {
      setUndoing(false)
    }
  }, [lastTurn, undoing, t])

  if (!enabled || !projectId || !Number.isSafeInteger(expectedGeneration) || (expectedGeneration ?? 0) <= 0) return null

  return (
    <div className="ze-copilot" data-side={side} data-open={open ? 'true' : 'false'} data-busy={busy ? 'true' : 'false'}>
      {/* 单一助手:小球已退役——精灵(GuideLayer)是唯一实体,点击精灵开合本面板。 */}
      <section className="ze-copilot__panel" role="dialog" aria-label={t('copilot.title')}>
        <header className="ze-copilot__bar">
          <span className="ze-copilot__pulse" aria-hidden="true" />
          <span className="ze-copilot__title">{t('copilot.title')}</span>
          <span className="ze-copilot__bar-actions">
            <button type="button" className="ze-button" data-variant="ghost" onClick={() => publishAssistant({ type: 'open_tours' })}>
              {t('guide.teachMe')}
            </button>
            <button type="button" className="ze-copilot__icon-btn" title={t('copilotAccount.title')} onClick={() => setAcctOpen(true)}>
              <ZeIcon name="user" size="xs" />
            </button>
            <button type="button" className="ze-copilot__icon-btn" title={t('copilot.move')} onClick={flipSide}>
              <ZeIcon name="drag" size="xs" />
            </button>
            <button type="button" className="ze-copilot__icon-btn" title={t('copilot.collapse')} onClick={() => setOpen(false)}>
              <ZeIcon name="close" size="xs" />
            </button>
          </span>
        </header>

        <div className="ze-copilot__transcript" ref={scrollRef}>
          {hasAccount === false ? (
            <div className="ze-copilot__empty">
              <div>{t('copilot.needBind')}</div>
              <button type="button" className="ze-button" data-variant="primary" style={{ marginTop: 8 }} onClick={() => setAcctOpen(true)}>
                {t('copilot.goConnect')}
              </button>
            </div>
          ) : messages.length === 0 && !busy ? (
            <div className="ze-copilot__empty">{t('copilot.empty')}</div>
          ) : null}
          {messages.map((m) => (
            <div key={m.id} className="ze-copilot__msg" data-role={m.role}>
              <div className="ze-copilot__bubble">
                {m.images && m.images.length > 0 ? (
                  <span className="ze-copilot__thumbs">
                    {m.images.map((src, i) => (
                      <img key={i} className="ze-copilot__thumb" src={src} alt="" />
                    ))}
                  </span>
                ) : null}
                {m.text ? <span>{m.text}</span> : null}
              </div>
            </div>
          ))}
          {busy ? (
            <div className="ze-copilot__msg" data-role="assistant">
              <div className="ze-copilot__bubble ze-copilot__bubble--thinking">
                <span className="ze-copilot__dots" aria-hidden="true"><i /><i /><i /></span>
                <span className="ze-copilot__thinking">{t('copilot.thinking')}</span>
              </div>
            </div>
          ) : null}
          {/* §7.3 P0-C:卡区经 AssistantStage 按 stageModel 裁定编排过程/变更/确认卡。
              turnState 由本组件的 busy + lastTurn 派生(SSE 逐事件 turn 态待后端接线,现按端到端态近似);
              connection 由 hasAccount 派生;confirmView 待确认流接线(现恒无)。 */}
          {(!busy || pending || liveStage) ? (
            <AssistantStage
              connection={{ available: hasAccount !== false, error: hasAccount === false ? 'requires_connection' : null }}
              turnState={(lastTurn ? 'completed' : null) as TurnState | null}
              change={lastTurn?.cs ?? null}
              confirmView={pending ? buildConfirmCardView(pending as ConfirmCardPayload) : null}
              liveStage={liveStage}
              undoing={undoing}
              onUndoTurn={undoLastTurn}
              onRevealEntity={(entityType, entityId, field) => publishAssistant({ type: 'reveal_entity', entityType, entityId, field })}
              onConfirmAction={(action) => {
                // 批准仍走精灵侧的既有网页 nonce 通道 —— 面板不另开第二条批准路径
                // (「AI 拿不到 nonce」这条红线只能有一个入口)。
                const nonce = String((pending as { nonce?: unknown } | null)?.nonce ?? '')
                publishAssistant({ type: 'confirm_action', action, nonce })
                if (action !== 'approve') setPending(null)
              }}
            />
          ) : null}
        </div>

        {hasAccount !== false ? (
        <footer className="ze-copilot__composer">
          {selected.length > 0 ? (
            <div className="ze-copilot__context">{t('copilot.selectionAttached', { count: selected.length })}</div>
          ) : null}
          {attachments.length > 0 ? (
            <div className="ze-copilot__attachments">
              {attachments.map((a) => (
                <span key={a.id} className="ze-copilot__chip">
                  <img className="ze-copilot__chip-img" src={a.dataUri} alt="" />
                  <button
                    type="button"
                    className="ze-copilot__chip-x"
                    title={t('copilot.removeImage')}
                    aria-label={t('copilot.removeImage')}
                    onClick={() => setAttachments((cur) => cur.filter((x) => x.id !== a.id))}
                  >
                    <ZeIcon name="close" size="xs" />
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          <div className="ze-copilot__input-row">
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => addFiles(e.target.files)}
            />
            <button
              type="button"
              className="ze-copilot__attach"
              disabled={busy || attachments.length >= MAX_IMAGES}
              onClick={() => fileRef.current?.click()}
              title={t('copilot.attachImage')}
              aria-label={t('copilot.attachImage')}
            >
              <ZeIcon name="image" size="sm" />
            </button>
            <textarea
              className="ze-copilot__input"
              rows={1}
              value={draft}
              placeholder={t('copilot.placeholder')}
              disabled={busy}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }}
            />
            <button
              type="button"
              className="ze-copilot__send"
              disabled={busy || (!draft.trim() && attachments.length === 0)}
              onClick={() => void send()}
              title={t('copilot.send')}
              aria-label={t('copilot.send')}
            >
              <ZeIcon name={busy ? 'loop' : 'chevron-up'} size="sm" spin={busy} />
            </button>
          </div>
        </footer>
        ) : null}
      </section>
      <CopilotAccountDialog
        open={acctOpen}
        onClose={() => setAcctOpen(false)}
        onChanged={notifyCredentialsChanged}
      />
    </div>
  )
}
