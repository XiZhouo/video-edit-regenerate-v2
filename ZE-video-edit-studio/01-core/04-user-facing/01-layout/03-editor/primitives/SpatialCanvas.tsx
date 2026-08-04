/**
 * SpatialCanvas — 变换舞台 + 屏幕空间 SVG overlay(框架级)
 *
 * 图像在 stage(CSS transform translate/scale,crisp 缩放);所有 box / gizmo / marquee 画在
 * 屏幕空间 SVG overlay(pointer-events:none,描边恒定 1px)。pointer 事件全归 container(bind),
 * 命中测试在图像空间(spatialMath),overlay 纯视觉。
 */

import type { ReactNode } from 'react'
import { gizmoHandlePoints, imageToScreen } from './spatialMath'
import { TransformGizmo } from './TransformGizmo'
import type { Box, Geometry, GizmoConstraints, HandleId, Viewport } from './spatialTypes'
import type { SpatialPointerBindings } from './useSpatialPointer'
import type { EntityRunStatus } from '../types'

export type SpatialBox = {
  id: string
  geom: Geometry
  selected: boolean
  /** F2 跨面板高亮(Rendering Hints):与 selected 正交、可叠加;overlay 画额外虚线描边。 */
  highlighted?: boolean
  label?: string
  status?: EntityRunStatus
}

export type SpatialCanvasProps = {
  src?: string
  viewport: Viewport
  boxes: SpatialBox[]
  /** 选中件的实时几何(= preview ?? 选中 box.geom);驱动 gizmo */
  selectedGeom?: Geometry | null
  constraints: GizmoConstraints
  activeHandle?: HandleId | null
  marquee?: Box | null
  containerRef: React.Ref<HTMLDivElement>
  bind: SpatialPointerBindings
  hud?: ReactNode
  onImageLoad?: (size: { w: number; h: number }) => void
  dataKind?: string
}

/** box 的 4 角屏幕坐标(rotation 已含)。 */
function corners(geom: Geometry, vp: Viewport): string {
  const p = gizmoHandlePoints(geom, 0)
  const a = ['nw', 'ne', 'se', 'sw'].map((h) => imageToScreen(p[h as HandleId], vp))
  return a.map((q) => `${q.x},${q.y}`).join(' ')
}

export function SpatialCanvas({
  src, viewport, boxes, selectedGeom, constraints, activeHandle, marquee,
  containerRef, bind, hud, onImageLoad, dataKind,
}: SpatialCanvasProps): ReactNode {
  return (
    <div
      ref={containerRef}
      className="ze-spatial"
      data-kind={dataKind}
      tabIndex={0}
      onPointerDown={bind.onPointerDown}
      onPointerMove={bind.onPointerMove}
      onPointerUp={bind.onPointerUp}
      onWheel={bind.onWheel}
      onDoubleClick={bind.onDoubleClick}
    >
      <div
        className="ze-spatial__checkerboard"
        style={{ transform: `translate(${viewport.tx}px, ${viewport.ty}px) scale(${viewport.scale})` }}
      >
        {src && (
          <img
            className="ze-spatial__img"
            src={src}
            alt=""
            draggable={false}
            onLoad={(e) => onImageLoad?.({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
          />
        )}
      </div>

      <svg className="ze-spatial__overlay" aria-hidden="true">
        {boxes.map((b) => (
          <polygon
            key={b.id}
            className="ze-spatial__box"
            data-selected={b.selected ? 'true' : undefined}
            data-status={b.status?.phase}
            points={corners(b.geom, viewport)}
          />
        ))}
        {/* F2 跨面板高亮:选中态(accent 实线)之上叠加 warn 虚线描边,两态可区分、可叠加。
            刻意用 inline SVG 属性而非 .ze-* class(消费既有 token,不 bump css 层)。 */}
        {boxes.filter((b) => b.highlighted).map((b) => (
          <polygon
            key={`hl:${b.id}`}
            points={corners(b.geom, viewport)}
            fill="none"
            stroke="var(--ze-semantic-warn)"
            strokeWidth={2}
            strokeDasharray="6 4"
          />
        ))}
        {selectedGeom && (
          <TransformGizmo frame={selectedGeom} viewport={viewport} constraints={constraints} activeHandle={activeHandle} />
        )}
        {marquee && (
          <polygon className="ze-spatial__marquee" points={corners({ ...marquee, rotation: 0 }, viewport)} />
        )}
      </svg>

      {hud && <div className="ze-spatial__hud">{hud}</div>}
    </div>
  )
}
