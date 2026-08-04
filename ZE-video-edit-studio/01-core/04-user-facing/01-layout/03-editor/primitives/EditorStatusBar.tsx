/**
 * EditorStatusBar — 编辑器局部状态条(框架级 primitive)
 *
 * 显示**本编辑器槽**的运行切片(running/queued 计数 + 最近操作 + 取消),与全局 footer
 * (服务器/同步/全局管线)是不同 scope,非重复。编辑器从 statusOf 派生计数,cancel 走
 * pipeline.cancel(由编辑器接 dispatch)。
 */

import { useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ProgressRing } from '../lib/Skeleton'
import { ZeIcon } from '../../lib/zeIcon'
import type { StatusSegment } from '../zones/types'

export type EditorStatusBarProps = {
  running: number
  queued: number
  latestOp?: string
  onCancel?: () => void
  /** 拓展点(zone 契约):额外状态段(领域计数);只显示,禁业务按钮。 */
  segments?: StatusSegment[]
}

function SegmentView({ seg, label }: { seg: StatusSegment; label: string }): ReactNode {
  if (seg.kind === 'progress') {
    const p = typeof seg.value === 'number' ? seg.value : Number(seg.value)
    return <ProgressRing progress={Number.isFinite(p) ? p : undefined} size={16} />
  }
  return (
    <span className="ze-badge" data-variant={seg.variant} data-size="sm">
      {label}{seg.value != null ? ` ${seg.value}` : ''}
    </span>
  )
}

export function EditorStatusBar({ running, queued, latestOp, onCancel, segments }: EditorStatusBarProps): ReactNode {
  const t = useT()
  const hasSegments = Boolean(segments && segments.length)
  // 可关闭功能区契约(functional-zone SPEC §7 collapsible)首落地:收起状态条 → 折到一个
  // 永远可见、点一下即复原的极简把手(不允许折到找不回)。纯视图态(useState,不进 doc / 不可 undo)。
  // 注:状态条是**瞬时活动 zone**(无活动即不渲染),收起仅作用于当前这段活动 —— 活动清空后整条
  // 自动隐去、折叠态随之复位,与 palette/inspector 等**常驻 zone**的会话级持久化(SPEC §7)不同。
  const [collapsed, setCollapsed] = useState(false)
  if (running === 0 && queued === 0 && !latestOp && !hasSegments) return null

  if (collapsed) {
    return (
      <button
        type="button"
        className="ze-editor__statusbar-reveal"
        data-active={running > 0 ? 'true' : undefined}
        title={t('editor.expandBar')}
        aria-label={t('editor.expandBar')}
        onClick={() => setCollapsed(false)}
      >
        <ZeIcon name="chevron-up" size="xs" />
        <span>{running > 0 ? `${t('editor.running')} ${running}` : t('editor.expandBar')}</span>
      </button>
    )
  }

  return (
    <div className="ze-editor__statusbar" data-zone="statusbar" role="status">
      <span className="ze-editor__statusbar-counts">
        <span className="ze-badge" data-variant={running > 0 ? 'info' : undefined} data-size="sm">
          {t('editor.running')} {running}
        </span>
        {queued > 0 && (
          <span className="ze-badge" data-size="sm">{t('editor.queued')} {queued}</span>
        )}
        {segments?.map((s) => (
          <SegmentView key={s.id} seg={s} label={s.label ?? (s.labelKey ? t(s.labelKey) : '')} />
        ))}
      </span>
      <span className="ze-editor__statusbar-op" title={latestOp}>{latestOp ?? t('editor.noOp')}</span>
      {onCancel && running > 0 && (
        <button type="button" className="ze-button" data-variant="ghost" data-size="sm" onClick={onCancel}>
          {t('editor.cancel')}
        </button>
      )}
      <button
        type="button"
        className="ze-icon-button ze-editor__statusbar-toggle"
        data-size="sm"
        data-variant="ghost"
        title={t('editor.collapseBar')}
        aria-label={t('editor.collapseBar')}
        onClick={() => setCollapsed(true)}
      ><ZeIcon name="chevron-down" size="xs" /></button>
    </div>
  )
}
