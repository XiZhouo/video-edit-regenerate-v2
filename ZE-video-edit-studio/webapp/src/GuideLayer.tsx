/**
 * GuideLayer — 引导精灵前端引擎(系统 3b『本相』;每个 ZE web 工具常驻,类 CopilotDock 挂载)。
 *
 * 消费系统 3a 已发的引导帧,把「指哪/说什么/点哪」画出来。三路驱动汇一处:
 *   1) 人点播 Help『教我用』→ fetchGuidedTours 选一条本地播(useGuideRuntime.playLocal);
 *   2) AI 实时 → ze_guide 逐帧经既有 SSE(guideBus)到达,精灵跟随;
 *   3) AI 挑 tour → 同 2,后端发 tour_id 对应的 guide_step 帧。
 *
 * 【AI 行为具象化(A3)】除教程外,精灵还是「AI 在行动」的身体(useAiActivity 聚合):
 *   - 改动回执:AI 经命令总线改实体/长任务起止 → 精灵飞过去点一下(单实体)或圈一下
 *     (多实体包围盒),F2 跨面板高亮同步点亮(App 高亮桥);1.2s 归并,教程优先;
 *   - 确认拉起:AI 发起高危命令 → 精灵圈住目标 + 气泡出「批准/先不」。批准走既有网页
 *     nonce 通道(cookie 专属端点取材料 → POST /api/command/confirm),AI 拿不到 nonce。
 *
 * 【治理红线】act/回执只出「幽灵点击」视觉;GuideLayer **从不合成 DOM 鼠标事件、从不执行
 * 命令**。真执行:AI 路已由 ze_guide/MCP 经命令总线跑完(dispatch source=ai + ACL/CAS +
 * 确认闸门),本地路则由用户自己点被指向的受管按钮。精灵只画视觉,总线永远在别处。
 *
 * 【系统 4 · 变形体 v2】精灵 = 单实体变形虫(spriteEngine.ts,设计真源《ZE精灵变形体视觉设计
 * -20260718》):白芯蓝边、球 ⇄ 光标 ⇄ 环形矩形三形态严格连续变形、贝塞尔弧线飞行 + 惯性转弯 +
 * 果冻拉伸 + canvas 余晖;圈选=本体长成实体环带(环外压暗)。8 态 FSM(guideMotion)保持不变,
 * 本层只把 FSM 稳态映射成引擎的 SpriteMode。纯表现:不改上面的红线。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { fetchGuidedTours, type GuideStep, type GuideTour } from './lib/api'
import { useGuideRuntime, type AnchorRect } from './lib/useGuideRuntime'
import { resolveGuideAnchor, type GuideSelection } from './lib/guideTargets'
import {
  deriveSettledState,
  formOverride,
  resolveState,
  stateForm,
  useGuideMotion,
  type GuideForm,
  type GuideState,
} from './lib/guideMotion'
import { SpriteEngine, parkPoint, type SpriteMode, type SpriteRectPx } from './lib/spriteEngine'
import { useAiActivity, useGuideConfig, type AiReceipt } from './lib/useAiActivity'
import { publishAssistant, subscribeAssistant } from '@ze/core/04-user-facing'

export type GuideLayerProps = {
  /** 当前打开项目 id;无项目 → 不挂(没有可引导的对象)。 */
  projectId?: string
  /** 跨编辑器选中(entity_type → active id);解析 entity:<type>:@selected 用。 */
  selection?: GuideSelection
  /** 项目世代 CAS(确认拉起的批准调用需要)。 */
  expectedGeneration?: number
  /** 由 config 控制;关则不挂(与 CopilotDock 同规矩,可后续接线)。 */
  enabled?: boolean
}

const SPRITE = 34
const HALF = SPRITE / 2
const GAP = 10
const MARGIN = 16

/** 引导文案(guide 命名空间),组件内经 useT 构建;stepNote 需要的子集见其签名。 */
type GuideCopy = {
  teachMe: string; next: string; prev: string; done: string; close: string
  pickTitle: string; empty: string; loading: string
  actAi: string; actLocal: string; wait: string; ask: string; confirmNote: string
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(Math.max(value, lo), hi)
}

function stepNote(step: GuideStep, L: GuideCopy): string {
  if (step.kind === 'act') return step.mode === 'act' ? L.actAi : L.actLocal
  if (step.kind === 'wait') return L.wait
  if (step.kind === 'ask') return L.ask
  return ''
}

/** FSM 稳态 → 引擎 SpriteMode(dismiss/hidden 由生命周期决定)。 */
function spriteModeOf(state: GuideState): SpriteMode {
  if (state === 'hidden') return 'hidden'
  if (state === 'dismissing') return 'dismiss'
  if (state === 'acting') return 'act'
  if (state === 'pointing') return 'point'
  if (state === 'framing') return 'frame'
  return 'rest'   // idle / appearing / moving(飞行由引擎自己演)
}

/**
 * 单一助手待机时的形态(W1-1):对话面板开着 → `hidden`;面板关着 → `rest`。
 *
 * **面板就是精灵本身。** 面板从精灵所在的那一点撑开(AssistantRoot 把
 * `--ze-assistant-origin-*` 钉在精灵翻转前的位置),所以精灵在那一刻退场、面板从那儿长出来,
 * 看到的就是「这个球变成了对话框」。精灵的存在感由面板头部那颗 18px 呼吸点承担
 * (`.ze-copilot__pulse`,同一套白芯蓝边材质)——那才是 0721 §3.2 阶段④说的「精灵在家的样子」。
 *
 * 这里原本给的是 `panel`,而引擎里 `panel` 的语义是「停靠到面板**旁边**、回圆球静息态」
 * (§7.3 P0-C「保留存在感但不抢戏」)。那一版与「精灵即面板」直接冲突:屏幕上会同时有
 * 面板和一个球。用户 2026-07-26 的判词:「我说的是这个球本身变成对话框」。两版设计只能留
 * 一版,以用户裁定为准 —— `panel` 模式就此没有生产者,引擎里那条分支与 SPRITE_PARAMS 的
 * panelDock/panelDockTop 一并成为死码,待清理批统一删。
 */
function idleModeOf(chatOpen: boolean): SpriteMode {
  return chatOpen ? 'hidden' : 'rest'
}

function elementRect(el: Element | null): AnchorRect {
  if (!el) return null
  const r = el.getBoundingClientRect()
  if (r.width <= 0 && r.height <= 0) return null
  return { top: r.top, left: r.left, width: r.width, height: r.height }
}

function unionRects(a: AnchorRect, b: AnchorRect): AnchorRect {
  if (!a) return b
  if (!b) return a
  const top = Math.min(a.top, b.top)
  const left = Math.min(a.left, b.left)
  const bottom = Math.max(a.top + a.height, b.top + b.height)
  const right = Math.max(a.left + a.width, b.left + b.width)
  return { top, left, width: right - left, height: bottom - top }
}

/** 回执目标集 → 包围盒(逐实体解析 entity:<type>:<id> 锚点;全无锚点 → null 只说不指)。 */
function receiptRect(receipt: AiReceipt, selection: GuideSelection): AnchorRect {
  let box: AnchorRect = null
  for (const group of receipt.entities) {
    for (const id of group.ids) {
      box = unionRects(box, elementRect(
        resolveGuideAnchor(`entity:${group.entityType}:${id}`, selection),
      ))
    }
  }
  return box
}

/**
 * SpriteStage — 变形体渲染引擎宿主(canvas 余晖 + 柔光 + 单 path SVG 由引擎自建自毁)。
 * 声明式输入 → 引擎内部 reconcile 成飞行/变形/圈选;纯装饰,aria-hidden。
 */
function SpriteStage({ mode, rect, rest, sound, thinking, onPos }: {
  mode: SpriteMode
  rect: SpriteRectPx | null
  rest: { x: number; y: number }
  sound: boolean
  thinking: boolean
  /** 停泊中心变了就报一次(W1-1:面板拿它当 transform-origin,从精灵那个点撑开)。 */
  onPos?: (x: number, y: number) => void
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const engineRef = useRef<SpriteEngine | null>(null)

  useEffect(() => {
    if (!hostRef.current) return
    const engine = new SpriteEngine(hostRef.current)
    engineRef.current = engine
    return () => {
      engine.destroy()
      engineRef.current = null
    }
  }, [])

  useEffect(() => {
    engineRef.current?.update({ mode, rect, rest, sound, thinking })
  })

  // 只在坐标真变时播,避免每次渲染都往总线上打(取整到像素;亚像素抖动不算变化)。
  useEffect(() => {
    onPos?.(Math.round(rest.x), Math.round(rest.y))
  }, [Math.round(rest.x), Math.round(rest.y), onPos])

  return <div ref={hostRef} className="ze-guide__stage" aria-hidden="true" />
}

export function GuideLayer({ projectId, selection, expectedGeneration, enabled = true }: GuideLayerProps) {
  const runtime = useGuideRuntime(selection)
  const { active, rect } = runtime

  // 过渡态时窗(appearing/moving);hook 须在早退前无条件调用。activeKey 变 → 触发一次过渡。
  const activeKey = active ? `${active.runId}#${active.index}` : null
  const transient = useGuideMotion(activeKey, runtime.ending)

  // AI 行为具象化:配置 + 聚合器(教程优先;确认拉起不受开关影响)。
  const guideConfig = useGuideConfig()
  const activity = useAiActivity({
    projectId,
    expectedGeneration,
    receiptEnabled: guideConfig.aiReceipt,
    tourActive: !!active,
  })

  // 单一助手协调:面板开合/停靠边/忙碌态经 assistantBus 到达;「教我用」由面板发起。
  const [chatOpen, setChatOpen] = useState(false)
  const [chatSide, setChatSide] = useState<'left' | 'right'>(
    () => (localStorage.getItem('ze.copilot.side') === 'left' ? 'left' : 'right'))
  const [assistantBusy, setAssistantBusy] = useState(false)
  // 面板矩形(AssistantRoot 量了播过来)。精灵的 panel 停靠形态要拿它算落点 ——
  // spriteEngine.update 里 `mode === 'rest' || !rect` 会早退,不给 rect 那个分支根本到不了。
  const [panelRect, setPanelRect] = useState<AnchorRect>(null)

  const [pickerOpen, setPickerOpen] = useState(false)
  const [tours, setTours] = useState<GuideTour[] | null>(null)
  const [loadingTours, setLoadingTours] = useState(false)
  // 窗口尺寸变化 / AI 活动展示期 → 强制重算锚点矩形(教程锚点由 runtime rAF 驱动)。
  const [, forceTick] = useState(0)
  const loadEpochRef = useRef(0)

  useEffect(() => {
    const onResize = () => forceTick((n) => n + 1)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // AI 活动(回执/确认拉起)展示期:低频刷新锚点(750ms;目标滚动/布局变化时环跟得上)。
  const hasActivity = !active && (!!activity.receipt || !!activity.pending)
  useEffect(() => {
    if (!hasActivity) return
    const timer = setInterval(() => forceTick((n) => n + 1), 750)
    return () => clearInterval(timer)
  }, [hasActivity])

  // 切项目 → 清引导态(旧 tour 无意义)。
  useEffect(() => {
    setPickerOpen(false)
    setTours(null)
    runtime.stop()
  // runtime.stop 稳定(useCallback []);只随 projectId 触发
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  const openPicker = useCallback(async () => {
    if (!projectId) return
    setPickerOpen(true)
    setLoadingTours(true)
    const epoch = ++loadEpochRef.current
    try {
      const list = await fetchGuidedTours(projectId)
      if (loadEpochRef.current === epoch) setTours(list)
    } catch {
      if (loadEpochRef.current === epoch) setTours([])
    } finally {
      if (loadEpochRef.current === epoch) setLoadingTours(false)
    }
  }, [projectId])

  const pick = useCallback((tour: GuideTour) => {
    setPickerOpen(false)
    runtime.playLocal(tour)
  }, [runtime])

  // W1-1:精灵停泊中心 → AssistantRoot(它写成 --ze-assistant-origin-*,面板据此撑开)。
  const publishSpritePos = useCallback((x: number, y: number) => {
    publishAssistant({ type: 'sprite_pos', x, y })
  }, [])

  useEffect(() => subscribeAssistant((frame) => {
    if (frame.type === 'chat_state') { setChatOpen(frame.open); setChatSide(frame.side) }
    else if (frame.type === 'busy') setAssistantBusy(frame.busy)
    else if (frame.type === 'open_tours') void openPicker()
    else if (frame.type === 'panel_rect') setPanelRect(frame.rect)
  }), [openPicker])

  // W1-5:待确认转投可读面板(它有 nonce/描述/影响/不可撤效果,够渲染整张卡);
  // 清空时也要播,否则面板上会留一张已经解决掉的卡。
  const pendingRef = useRef<unknown>(null)
  useEffect(() => {
    const payload = (activity.pending?.payload ?? null) as Record<string, unknown> | null
    if (payload === pendingRef.current) return
    pendingRef.current = payload
    publishAssistant({ type: 'pending_confirm', payload })
  }, [activity.pending])

  // 面板上按的三个动作,回到这里执行 —— 批准仍走 useAiActivity 的既有网页 nonce 通道,
  // 不在面板侧另开一条批准路径(AI 拿不到 nonce 这条红线不能有第二个入口)。
  useEffect(() => subscribeAssistant((frame) => {
    if (frame.type !== 'confirm_action') return
    if (frame.action === 'approve') void activity.approve()
    else activity.dismissPending()      // later / cancel 都是「现在不批」,区别只在文案
  }), [activity])

  const t = useT()
  const L: GuideCopy = {
    teachMe: t('guide.teachMe'),
    next: t('guide.next'),
    prev: t('guide.prev'),
    done: t('guide.done'),
    close: t('guide.close'),
    pickTitle: t('guide.pickTitle'),
    empty: t('guide.empty'),
    loading: t('guide.loading'),
    actAi: t('guide.actAi'),
    actLocal: t('guide.actLocal'),
    wait: t('guide.wait'),
    ask: t('guide.ask'),
    confirmNote: t('guide.confirmNote'),
  }

  if (!enabled || !projectId) return null

  const step = active?.step ?? null
  const kind = step?.kind

  // ── 8 态 FSM + 形态(纯派生:step 语义 + 有无锚点 + 收尾余晖)──────────────────
  const override = step ? formOverride(step.presentation) : null
  const settledState = deriveSettledState(kind, !!rect, override)
  const state: GuideState = resolveState(!!active, runtime.ending, transient, settledState)
  const form: GuideForm = stateForm(state, kind, !!rect, override)

  const vw = window.innerWidth
  const vh = window.innerHeight

  // ── AI 活动锚点(仅无教程时驱动精灵;教程永远优先)────────────────────────
  const pendingPayload = activity.pending?.payload ?? null
  let activityMode: SpriteMode | null = null
  let activityRect: AnchorRect = null
  let activitySay = ''
  if (!active && pendingPayload) {
    const ref = pendingPayload.entity_ref
    const target = ref && (ref.entity_type || ref.entity_id)
      ? `entity:${ref.entity_type}:${ref.entity_id}`
      : `cmd:${pendingPayload.command_id}`
    activityRect = elementRect(resolveGuideAnchor(target, selection ?? {}))
    activityMode = activityRect ? 'frame' : 'rest'
    activitySay = pendingPayload.summary
      || t('guide.pendingAsk', { command: pendingPayload.command_id })
  } else if (!active && activity.receipt) {
    const receipt = activity.receipt
    activityRect = receiptRect(receipt, selection ?? {})
    const single = receipt.note === 'edited' && receipt.count === 1
    activityMode = activityRect ? (single ? 'act' : 'frame') : 'rest'
    activitySay = receipt.note === 'edited'
      ? (single ? t('guide.aiEditedOne') : t('guide.aiEditedMany', { count: receipt.count }))
      : t(`guide.${receipt.note}`)
  }

  // ── 精灵目标态 + 停靠/气泡锚点(视口坐标)────────────────────────────────
  const idleMode: SpriteMode = idleModeOf(chatOpen)
  // 待机且面板开着时,锚点就是面板本身 —— 精灵停到它边上(0721 §3.2 阶段④「驻留」)。
  const anchorRect: AnchorRect = active ? rect
    : (activityRect ?? (idleMode === 'panel' ? panelRect : null))
  // 停靠中心:有锚点 → 近目标(say 也停在目标旁说);无锚点 → 助手停靠位(随对话面板同侧角落;
  // 面板展开时上移到面板上方,单一助手与它的对话框在一处)。
  const panelH = chatOpen ? Math.min(560, vh - 96) : 0
  const dockX = chatSide === 'left' ? MARGIN + HALF : vw - MARGIN - HALF
  const dockY = clamp(vh - 20 - HALF - panelH - (chatOpen ? 26 : 0), MARGIN + HALF, vh - MARGIN - HALF)
  let restPt = { x: dockX, y: dockY }
  if (anchorRect) {
    const left = clamp(anchorRect.left, MARGIN, vw - SPRITE - MARGIN)
    const above = anchorRect.top - SPRITE - GAP
    const top = above >= MARGIN
      ? above
      : clamp(anchorRect.top + anchorRect.height + GAP, MARGIN, vh - SPRITE - MARGIN)
    restPt = { x: left + HALF, y: top + HALF }
  }
  const tourMode: SpriteMode = spriteModeOf(state)
  const mode: SpriteMode = active ? tourMode : (activityMode ?? idleMode)
  // 气泡锚点随「稳态」park(dismiss 期间不跳走);引擎拿真实 mode(含 dismiss)。
  // 与 mode 同口径 —— 此前这里恒 'rest',和上面的 mode 不一致(气泡与本体会按两套规则停)。
  const anchorMode: SpriteMode = active ? spriteModeOf(settledState) : (activityMode ?? idleMode)
  const park = parkPoint(anchorMode, anchorRect, restPt)
  const bubbleLeft = clamp(park.x - HALF, MARGIN, vw - SPRITE - MARGIN)
  const bubbleTop = clamp(park.y - HALF, MARGIN, vh - SPRITE - MARGIN)
  const hside = bubbleLeft > vw * 0.5 ? 'left' : 'right'
  const vside = bubbleTop < vh * 0.45 ? 'down' : 'up'
  const idle = !active && !activityMode

  return (
    <div className="ze-guide" aria-live="polite">
      {/* 变形体渲染引擎(常驻宿主;教程/AI 活动驱动;待机时按面板开合取 rest/panel) */}
      <SpriteStage
        mode={mode}
        rect={anchorRect}
        rest={restPt}
        sound={guideConfig.sound}
        thinking={assistantBusy}
        onPos={publishSpritePos}
      />

      {/* 单一助手的**本体即按钮**(W1-1)。
          此前这里是一个独立的 44×44 `ze-guide__hotspot`,它与引擎画出来的精灵是两个东西:
          实测点开面板时热点从 (1385,841) 瞬移到 (1385,255)、而精灵本体留在原地——
          屏幕上于是有两个圆,这正是「分身」的第二处来源。现在只留这一个,它就贴在精灵
          身上(同一 restPt),视觉全归引擎,它只提供命中区与可及性。 */}
      {idle ? (
        <button
          type="button"
          className="ze-guide__self"
          style={{ left: restPt.x, top: restPt.y }}
          aria-label={t('copilot.title')}
          title={t('copilot.title')}
          onClick={() => publishAssistant({ type: 'toggle_chat' })}
        />
      ) : null}
      {pickerOpen ? (
        <div className="ze-guide__picker" data-side={chatSide} role="menu" aria-label={L.pickTitle}>
          {loadingTours ? (
            <div className="ze-guide__picker-empty">{L.loading}</div>
          ) : (tours && tours.length > 0) ? (
            tours.map((tour) => (
              <button
                key={tour.id}
                type="button"
                className="ze-guide__picker-item"
                role="menuitem"
                onClick={() => pick(tour)}
              >
                <span className="ze-guide__picker-title">{tour.title || tour.id}</span>
                {tour.when ? <span className="ze-guide__picker-when">{tour.when}</span> : null}
              </button>
            ))
          ) : (
            <div className="ze-guide__picker-empty">{L.empty}</div>
          )}
        </div>
      ) : null}

      {/* 教程进行中:气泡(锚在精灵停泊点;精灵本体由 SpriteStage 引擎驱动) */}
      {active && step ? (
        <div
          className="ze-guide__sprite"
          data-state={state}
          data-form={form}
          style={{ transform: `translate3d(${bubbleLeft}px, ${bubbleTop}px, 0)` }}
        >
          <div className="ze-guide__bubble" data-hside={hside} data-vside={vside} role="status">
            <div className="ze-guide__meta">
              {t('guide.stepCounter', { current: active.index + 1, total: active.total })}
            </div>
            {step.say ? <div className="ze-guide__say">{step.say}</div> : null}
            {stepNote(step, L) ? <div className="ze-guide__note">{stepNote(step, L)}</div> : null}
            {step.confirm ? <div className="ze-guide__note">{L.confirmNote}</div> : null}
            <div className="ze-guide__actions">
              {runtime.canPrev ? (
                <button
                  type="button"
                  className="ze-button"
                  data-variant="ghost"
                  onClick={runtime.prev}
                >
                  {L.prev}
                </button>
              ) : null}
              {runtime.isLocal ? (
                <button
                  type="button"
                  className="ze-button"
                  data-variant="primary"
                  onClick={runtime.canNext ? runtime.next : runtime.stop}
                >
                  {runtime.canNext ? L.next : L.done}
                </button>
              ) : (
                <button
                  type="button"
                  className="ze-button"
                  data-variant="ghost"
                  onClick={runtime.stop}
                >
                  {L.close}
                </button>
              )}
            </div>
          </div>
        </div>
      ) : null}

      {/* AI 确认拉起:气泡出「批准/先不」;批准走既有网页 nonce 通道(纯治理面 UI) */}
      {!active && pendingPayload ? (
        <div
          className="ze-guide__sprite"
          data-state="framing"
          data-form="frame"
          style={{ transform: `translate3d(${bubbleLeft}px, ${bubbleTop}px, 0)` }}
        >
          <div className="ze-guide__bubble" data-hside={hside} data-vside={vside} role="status">
            <div className="ze-guide__meta">{t('guide.pendingMeta')}</div>
            <div className="ze-guide__say">{activitySay}</div>
            <div className="ze-guide__note">{t('guide.pendingNeed')}</div>
            {activity.pending?.error ? (
              <div className="ze-guide__note" data-tone="danger">
                {t('guide.approveFailed', { msg: activity.pending.error })}
              </div>
            ) : null}
            <div className="ze-guide__actions">
              <button
                type="button"
                className="ze-button"
                data-variant="ghost"
                onClick={activity.dismissPending}
              >
                {t('guide.later')}
              </button>
              <button
                type="button"
                className="ze-button"
                data-variant="primary"
                disabled={activity.pending?.busy}
                onClick={activity.approve}
              >
                {t('guide.approve')}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* AI 改动/任务回执:轻气泡只说一句,不带按钮(展示期自清) */}
      {!active && !pendingPayload && activity.receipt ? (
        <div
          className="ze-guide__sprite"
          data-state={activityMode === 'act' ? 'acting' : 'framing'}
          data-form={activityMode === 'act' ? 'cursor' : 'frame'}
          style={{ transform: `translate3d(${bubbleLeft}px, ${bubbleTop}px, 0)` }}
        >
          <div className="ze-guide__bubble" data-hside={hside} data-vside={vside} role="status">
            <div className="ze-guide__say">{activitySay}</div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
