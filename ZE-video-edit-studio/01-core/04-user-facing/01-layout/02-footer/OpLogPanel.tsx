/**
 * OpLogPanel — 展开主体里的"最近 op 日志"section
 *
 * SPEC §2.3:最新在顶,固定显示 30 条,超出滚动。
 *
 * 视觉规则全在 footer.css(.ze-footer__oplog / .ze-footer__oplog-entry)
 */

import { type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useScrollAffordance } from '../03-editor/lib/useScrollAffordance'
import { ZeIcon } from '../lib/zeIcon'
import type { OpLogEntry } from './types'

export type OpLogPanelProps = {
  entries: OpLogEntry[]
  max?: number
  /** undo/redo 可用态 + 触发;onUndo/onRedo 全缺省时不渲染按钮(模板态)*/
  canUndo?: boolean
  canRedo?: boolean
  onUndo?: () => void
  onRedo?: () => void
  /** tab 模式:标题由外层 tab 提供,隐藏自带标题(仅保留 undo/redo 控件)*/
  hideTitle?: boolean
}

export function OpLogPanel(props: OpLogPanelProps): ReactNode {
  const { entries, max = 30, canUndo, canRedo, onUndo, onRedo, hideTitle } = props
  const t = useT()
  const bodyRef = useScrollAffordance('y')

  // 入参约定:已倒序;这里只 slice 限制条数
  const visible = entries.slice(0, max)
  const showHistoryControls = Boolean(onUndo || onRedo)

  return (
    <section className="ze-footer__section" data-flex="oplog">
      {(!hideTitle || showHistoryControls) && (
      <header className="ze-footer__section-header">
        {!hideTitle && <span>{t('footer.recentOps')}</span>}
        {!hideTitle && (
          <span className="ze-footer__section-header-count">{entries.length}</span>
        )}
        {showHistoryControls && (
          // marginLeft:auto 结构布局内联(无颜色)— 用 L2 .ze-icon-button(xs),不自造
          <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: '2px' }}>
            <button
              type="button"
              className="ze-icon-button"
              data-size="xs"
              disabled={!canUndo}
              aria-disabled={!canUndo ? 'true' : undefined}
              onClick={onUndo}
              title={t('footer.undoTitle')}
              aria-label={t('footer.undo')}
            >
              <ZeIcon name="undo" size="xs" />
            </button>
            <button
              type="button"
              className="ze-icon-button"
              data-size="xs"
              disabled={!canRedo}
              aria-disabled={!canRedo ? 'true' : undefined}
              onClick={onRedo}
              title={t('footer.redoTitle')}
              aria-label={t('footer.redo')}
            >
              <ZeIcon name="redo" size="xs" />
            </button>
          </span>
        )}
      </header>
      )}
      <div className="ze-footer__section-body ze-scroll-fade-y" ref={bodyRef}>
        {visible.length === 0 ? (
          <div className="ze-footer__empty">{t('footer.noOps')}</div>
        ) : (
          <div className="ze-footer__oplog">
            {visible.map((entry) => (
              <OpLogRow key={entry.id} entry={entry} />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}


function OpLogRow({ entry }: { entry: OpLogEntry }): ReactNode {
  const t = useT()
  const rowTitle =
    t('footer.opRowTitle', { cmd: entry.command_id, trace: entry.trace_id }) +
    (entry.error_message ? `\n${entry.error_message}` : '')
  return (
    <div
      className="ze-footer__oplog-entry"
      data-source={entry.source}
      data-status={entry.status}
      title={rowTitle}
    >
      <span className="ze-footer__oplog-time">{formatTime(entry.timestamp)}</span>
      <span
        className="ze-footer__oplog-source"
        data-source={entry.source}
        title={t('footer.sourceLabel', { source: entry.source })}
      >
        {entry.source.charAt(0)}
      </span>
      <span className="ze-footer__oplog-label">{entry.command_label}</span>
      <span
        className="ze-footer__oplog-status"
        data-status={entry.status}
        title={t('footer.statusLabel', { status: entry.status })}
        aria-label={entry.status}
      />
      <span className="ze-footer__oplog-duration">
        {formatDuration(entry.duration_ms)}
      </span>
    </div>
  )
}


function formatTime(ms: number): string {
  const d = new Date(ms)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

function formatDuration(ms: number): string {
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60_000)
  const s = Math.floor((ms % 60_000) / 1000)
  return `${m}m${s}s`
}
