/**
 * DialogEditor — `dialog` editor class:AI 对话编辑器(transcript 内容区 + 发送)。
 *
 * 多实体 master:把 message 实体集渲成按序气泡(role 驱动左右/配色),底部 composer 发送。
 * transcript = Domain Data(message 实体,可 undo/可重载/SSE 驱动);发送 = entity.add 一条 user
 * 消息(走命令通道、可撤销),mod 声明 replyCommand 时再派发触发 AI 回复/生成。
 *
 * 受控件:entities 进、dispatch 出;不持有持久态(草稿 text 是本地输入态)。promote 自
 * chat-canvas-studio chatPane custom 渲染器为 core 类。**反问双向协议(question 实体 + dialog.answer
 * stage 暂挂续跑)+ 终端子面(PTY-over-WS)是后续项**,本类先落 transcript+send 基座。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ZeIcon } from '../../../lib/zeIcon'
import { useScrollAffordance } from '../../lib/useScrollAffordance'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type DialogEditorProps = {
  /** 标准 public class 用 conversation;dialog 保留为兼容 alias。 */
  editorClass?: 'dialog' | 'conversation'
  entityType: string
  /** 编辑器名(layout slot.name;优先于 typeLabel 回退)。 */
  editorName?: string
  typeLabel?: string
  /** message 实体集(适配层注入;按 orderField 排序由编辑器做)。 */
  entities: EntityDict[]
  /** 角色字段(值如 'user'/'assistant';驱动气泡左右+配色)。缺省 'role'。 */
  roleField?: string
  /** 正文字段。缺省 'text'。 */
  textField?: string
  /** 排序字段(number;升序)。缺省 'order'。 */
  orderField?: string
  /** 发送方消息的 role 值(气泡靠右、accent)。缺省 'user'。 */
  userRole?: string
  /** 发送一条消息后额外派发的命令(触发 AI 回复/生成;{ text, ... });不声明则只记 user 消息。 */
  replyCommand?: string
  /** replyCommand 的正文参数名;缺省 text,如页面生成可设 prompt。 */
  replyTextArg?: string
  /** replyCommand 的额外参数(如当前 page id),由装配层从 view_state/secondary binding 解析。 */
  replyArgs?: Record<string, unknown>
  /** replyCommand 的**发送时刻**求值参数(如当前选中作用域)——点发送才取值,与静态 replyArgs 合并。 */
  replyArgsLive?: () => Record<string, unknown>
  /** composer 上方常驻「编辑目标」条(装配层传数据,本层渲染——分层纪律:原子类只在编辑器层用)。
   *  names=当前选中实体名(实时);onClear 清空选择;names 空时显示 emptyLabel。 */
  replyScope?: { label: string; emptyLabel: string; clearLabel: string; names: string[]; onClear?: () => void }
  /** 写入 user message 的额外 data(如 pageId),由装配层解析。 */
  messageExtraData?: Record<string, unknown>
  /** 运行态:pending/running 显"生成中"占位气泡。 */
  status?: EntityRunStatus
  /** 注入功能区:content 之前/之后。用于 proposal apply 等对话附属动作。 */
  extraZones?: ReactNode[]
  afterZones?: ReactNode[]
  afterSize?: 'hug' | 'share'
  afterGrow?: number
  dispatch?: DispatchFn
}

function str(v: unknown): string { return typeof v === 'string' ? v : '' }
function numOf(v: unknown): number { return typeof v === 'number' ? v : 0 }

export function DialogEditor({
  editorClass = 'dialog',
  entityType, editorName, typeLabel, entities, roleField = 'role', textField = 'text', orderField = 'order',
  userRole = 'user', replyCommand, replyTextArg = 'text', replyArgs, replyArgsLive, replyScope, messageExtraData, status,
  extraZones, afterZones, afterSize, afterGrow, dispatch,
}: DialogEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const [draft, setDraft] = useState('')
  const phase = status?.phase
  const pending = phase === 'running' || phase === 'pending'

  const messages = useMemo(
    () => [...entities].filter((m) => !m.deleted).sort((a, b) => numOf(a.data[orderField]) - numOf(b.data[orderField])),
    [entities, orderField],
  )

  // 新消息/生成态 → 滚到底。
  const listRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight
  }, [messages.length, pending])
  // 滚动边缘提示与「滚到底」共用同一容器:合并两个 ref。
  const fadeRef = useScrollAffordance('y')
  const transcriptRef = useCallback((el: HTMLDivElement | null) => {
    listRef.current = el
    fadeRef(el)
  }, [fadeRef])

  const send = () => {
    const text = draft.trim()
    if (!text || pending) return
    setDraft('')
    const order = Math.max(-1, ...messages.map((m) => numOf(m.data[orderField]))) + 1
    // 1) 记一条 user 消息(entity.add,source=user;可撤销/可重载)。
    Promise.resolve(
      fire('entity.add', {
        type: entityType, name: text.slice(0, 40),
        data: { [roleField]: userRole, [textField]: text, [orderField]: order, ...(messageExtraData ?? {}) },
      }),
      // 2) mod 声明了 replyCommand → 触发 AI 回复/生成(stage;真改动由 supervisor 提交回)。
    ).then(() => { if (replyCommand) fire(replyCommand, { ...(replyArgs ?? {}), ...(replyArgsLive?.() ?? {}), [replyTextArg]: text }) })
  }

  return (
    <EditorFrame
      editorClass={editorClass}
      header={<HeaderZone title={editorName ?? typeLabel ?? entityType} count={pending ? t('editor.generating') : `${messages.length}`} />}
      zones={extraZones?.length ? extraZones : undefined}
      afterZones={afterZones}
      afterSize={afterSize}
      afterGrow={afterGrow}
    >
      <>
        <div
          className="ze-dialog__transcript ze-scroll-fade-y ze-gap ze-pad" data-gap="1" data-p="2"
          ref={transcriptRef} role="log" aria-live="polite"
        >
          {messages.length === 0 && !pending ? (
            <div className="ze-editor__empty">{t('editor.dialogEmpty')}</div>
          ) : null}
          {messages.map((m) => {
            const role = str(m.data[roleField]) || 'assistant'
            const self = role === userRole
            return (
              <div
                key={m.id}
                className="ze-dialog__bubble ze-surface ze-edge ze-round ze-pad ze-gap"
                data-surface={self ? 'accent-wash' : 'sunken'} data-edge="all" data-radius="md" data-p="2" data-gap="1"
                data-role={role} data-self={self ? 'true' : undefined}
              >
                <span className="ze-dialog__role ze-text" data-role="tertiary" data-size="xs">{role}</span>
                <span className="ze-dialog__text ze-text">{str(m.data[textField])}</span>
              </div>
            )
          })}
          {pending ? (
            <div
              className="ze-dialog__bubble ze-surface ze-edge ze-round ze-pad ze-gap"
              data-surface="sunken" data-edge="all" data-radius="md" data-p="2" data-gap="1"
              data-role="assistant" data-pending="true"
            >
              <ZeIcon name="loop" size="xs" spin /> <span className="ze-dialog__text ze-text">{t('editor.generating')}</span>
            </div>
          ) : null}
        </div>
        {replyScope ? (
          <div className="ze-row ze-edge ze-pad" data-edge="top" data-gap="1" data-p="1" data-align="center" data-wrap="true">
            <span className="ze-text" data-role="secondary" data-size="xs">{replyScope.label}</span>
            {replyScope.names.length ? (
              <>
                {replyScope.names.map((nm, i) => (
                  <span key={`${nm}-${i}`} className="ze-badge" data-variant="info" data-size="sm">{nm}</span>
                ))}
                {replyScope.onClear ? (
                  <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={replyScope.onClear}>
                    {replyScope.clearLabel}
                  </button>
                ) : null}
              </>
            ) : (
              <span className="ze-text" data-role="secondary" data-size="xs">{replyScope.emptyLabel}</span>
            )}
          </div>
        ) : null}
        <div className="ze-dialog__composer ze-edge ze-gap ze-pad" data-edge="top" data-gap="2" data-p="2">
          <textarea
            className="ze-textarea ze-dialog__input" data-size="sm"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            placeholder={t('editor.dialogPlaceholder')}
            rows={2}
            disabled={pending}
          />
          <button
            type="button" className="ze-button" data-variant="primary" data-size="sm"
            disabled={pending || !draft.trim()} onClick={send}
          >
            {t('editor.dialogSend')}
          </button>
        </div>
      </>
    </EditorFrame>
  )
}
