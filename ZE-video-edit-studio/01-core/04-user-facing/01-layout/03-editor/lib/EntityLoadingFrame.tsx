/**
 * EntityLoadingFrame — 统一、尺寸稳定的 entity 加载/处理态外壳(框架级,随 03-editor 一体复制)
 *
 * 解决「每个 entity 在 加载 / 运算 / AI 处理 时都有一致动画、且前后同尺寸(一张图/一张卡加载前后
 * 本身一样大,只是显示效果不同)」。做法:外层先把盒子尺寸定死(aspectRatio / height / minHeight),
 * 内层据 phase 切 骨架 / 内容 / 错误 / busy 叠加,四态共用同一盒子 → load 前后零 reflow。
 *
 * - pending → 骨架填满 footprint(默认 SkeletonLines,可传自定义 skeleton)
 * - running / busy → 保留 children(原内容),**同尺寸**叠一层处理 veil + 进度环(不塌缩、不替换成小 spinner)
 * - error → ErrorPlaceholder(compact,落在 footprint 内)
 * - done / 无状态 → children(真实内容,盒子不变)
 *
 * 视觉类 .ze-entity-frame / .ze-entity-busy 在皮肤(03-css-rules/02-atoms/placeholder.css)。
 * 占位尺寸契约见 03-editor/SPEC.md:占位必须复用加载后内容的 footprint,禁止「先小后大」。
 */

import type { CSSProperties, ReactNode } from 'react'
import { ProgressRing, ErrorPlaceholder, SkeletonLines } from './Skeleton'
import type { EntityRunStatus } from '../types'

export type EntityLoadingFrameProps = {
  status?: EntityRunStatus
  /** AI 处理叠加:保留内容、同尺寸盖一层 veil(与 running 等效,供非 pipeline 的 busy 用)。 */
  busy?: boolean
  onRetry?: () => void
  /** footprint 锁定(三选一/可叠加;都是几何,内联合法)。 */
  aspectRatio?: number | string
  height?: number | string
  minHeight?: number | string
  /** pending 时的骨架(默认 SkeletonLines);应与 children 同尺寸。 */
  skeleton?: ReactNode
  className?: string
  children?: ReactNode
}

export function EntityLoadingFrame({
  status, busy, onRetry, aspectRatio, height, minHeight, skeleton, className, children,
}: EntityLoadingFrameProps): ReactNode {
  const phase = status?.phase
  const style: CSSProperties = {}
  if (aspectRatio !== undefined) style.aspectRatio = String(aspectRatio)
  if (height !== undefined) style.height = height
  if (minHeight !== undefined) style.minHeight = minHeight

  const cls = className ? `ze-entity-frame ${className}` : 'ze-entity-frame'
  const processing = phase === 'running' || busy

  let body: ReactNode
  if (phase === 'error') {
    body = <ErrorPlaceholder compact error={status?.error} onRetry={onRetry} />
  } else if (phase === 'pending') {
    body = skeleton ?? <SkeletonLines lines={3} />
  } else {
    body = (
      <>
        {children}
        {processing && (
          <div className="ze-entity-busy" aria-hidden="true">
            <ProgressRing progress={status?.progress} size={36} label={status?.stage_label} />
          </div>
        )}
      </>
    )
  }

  return (
    <div className={cls} style={style} data-run-phase={phase ?? 'idle'} data-busy={processing ? 'true' : undefined}>
      {body}
    </div>
  )
}
