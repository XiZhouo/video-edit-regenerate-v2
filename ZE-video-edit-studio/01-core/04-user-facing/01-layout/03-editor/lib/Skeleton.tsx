/**
 * Skeleton — 加载 / 进度 / 错误占位 primitive(框架级,随 03-editor 一体复制)
 *
 * 协议: 03-editor/SPEC.md(加载形态)+ Block1 加载 / 管线 / 占位系统。
 * 视觉: 03-css-rules/02-atoms/{feedback,placeholder}.css 的 .ze-skeleton /
 *       .ze-progress-ring / .ze-error-placeholder(全走 L1 token,主题自适应)。
 *
 * 占位不是新系统:editor 据某 entity 的 EntityRunStatus.phase 选渲染——
 *   pending / running → 骨架(+ 可选进度环);error → ErrorPlaceholder(可重试);
 *   done / 无状态 → 真实内容。各 editor class 用这些 primitive 拼出与自身形态匹配的占位。
 */

import type { CSSProperties, ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import type { RunError } from '../types'

export function SkeletonText({ width }: { width?: number | string }): ReactNode {
  const style: CSSProperties | undefined = width === undefined ? undefined : { width }
  return <span className="ze-skeleton" data-variant="text" style={style} />
}

export function SkeletonBlock({ height }: { height?: number | string }): ReactNode {
  const style: CSSProperties | undefined = height === undefined ? undefined : { height }
  return <span className="ze-skeleton" data-variant="block" style={style} />
}

export function SkeletonCircle({ size = 24 }: { size?: number }): ReactNode {
  return <span className="ze-skeleton" data-variant="circle" style={{ width: size, height: size }} />
}

/** n 行文本骨架(列表行 / 表单字段 / 卡片标题用)。最后一行偏窄,更像真实文本。 */
export function SkeletonLines({ lines = 3 }: { lines?: number }): ReactNode {
  return (
    <span className="ze-skeleton-lines" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <span
          key={i}
          className="ze-skeleton"
          data-variant="text"
          style={i === lines - 1 && lines > 1 ? { width: '60%' } : undefined}
        />
      ))}
    </span>
  )
}

/** 圆环进度(viewport / 缩略图叠加用)。progress 0..1;undefined = indeterminate 旋转。 */
export function ProgressRing({
  progress, size = 36, label,
}: { progress?: number; size?: number; label?: string }): ReactNode {
  const indeterminate = progress === undefined || Number.isNaN(progress)
  const pct = indeterminate ? 0.25 : Math.max(0, Math.min(1, progress))
  const r = (size - 4) / 2
  const circ = 2 * Math.PI * r
  return (
    <span
      className="ze-progress-ring"
      data-indeterminate={indeterminate ? 'true' : 'false'}
      role="progressbar"
      aria-valuenow={indeterminate ? undefined : Math.round(pct * 100)}
      aria-label={label ?? 'progress'}
      style={{ width: size, height: size }}
    >
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size}>
        <circle className="ze-progress-ring__track" cx={size / 2} cy={size / 2} r={r} fill="none" />
        <circle
          className="ze-progress-ring__fill"
          cx={size / 2} cy={size / 2} r={r} fill="none"
          strokeDasharray={circ}
          strokeDashoffset={circ * (1 - pct)}
        />
      </svg>
      {!indeterminate && <span className="ze-progress-ring__pct">{Math.round(pct * 100)}</span>}
      {label && <span className="ze-progress-ring__label">{label}</span>}
    </span>
  )
}

/** 错误占位:红框 + 消息 + 可选重试按钮。compact 用于表格行内 / 卡片内。 */
export function ErrorPlaceholder({
  error, onRetry, compact,
}: { error?: RunError; onRetry?: () => void; compact?: boolean }): ReactNode {
  const t = useT()
  return (
    <div className="ze-error-placeholder" data-compact={compact ? 'true' : 'false'} role="alert">
      <span className="ze-error-placeholder__icon ze-icon" data-icon="warn" aria-hidden="true" />
      <span className="ze-error-placeholder__msg" title={error?.message}>{error?.message ?? t('editor.genFailed')}</span>
      {onRetry && (
        <button type="button" className="ze-button" data-variant="ghost" data-size="sm" onClick={onRetry}>
          {t('editor.retry')}
        </button>
      )}
    </div>
  )
}
