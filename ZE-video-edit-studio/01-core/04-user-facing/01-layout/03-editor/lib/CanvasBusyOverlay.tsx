/**
 * CanvasBusyOverlay — canvas 系单实体编辑器(asset/media/scene3d/canvas2d)统一的"处理中"覆盖层。
 *
 * 四个编辑器原各自内联同一段 `.ze-editor__canvas-overlay + ProgressRing`;在此收口成一个组件,
 * 保证加载动画风格 / 尺寸完全一致。覆盖层绝对定位填满 `.ze-editor__canvas-body`(flex:1,footprint
 * 由 body 锁定)→ 加载前后零 reflow。pending/running 渲染**内容形骨架**(块,填满盒)+ 进度环,
 * 不再是孤零零一个 spinner → "占位 = 加载后内容的形状,只是可视化不同"。
 *
 * 只渲染 pending/running 覆盖层;loaded/error 由各编辑器自己决定(content vs ErrorPlaceholder),
 * 故 loaded DOM(视口 / 空间画布 / video / 渲染器宿主)不受影响。
 * 视觉类 .ze-editor__canvas-overlay / .ze-editor__canvas-skeleton 在 04-editors/editor.css(共享 body 脚手架)。
 */

import type { ReactNode } from 'react'
import { ProgressRing } from './Skeleton'
import type { EntityRunStatus } from '../types'

export function CanvasBusyOverlay({ status }: { status?: EntityRunStatus }): ReactNode {
  const phase = status?.phase
  if (phase !== 'pending' && phase !== 'running') return null
  return (
    <div className="ze-editor__canvas-overlay">
      <span className="ze-skeleton ze-editor__canvas-skeleton" data-variant="block" aria-hidden="true" />
      <ProgressRing progress={status?.progress} size={48} label={status?.stage_label} />
    </div>
  )
}
