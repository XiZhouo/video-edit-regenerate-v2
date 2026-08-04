/**
 * TransformGizmo — 8 把手 + 旋转臂(框架级,纯渲染 SVG 片段)
 *
 * 画在屏幕空间的 SVG overlay 里(非 stage 内),故描边/把手恒定屏幕大小、不随 zoom 糊。
 * 不持指针——pointer 逻辑在 useSpatialPointer;本组件只据 frame + viewport 摆位。
 * 颜色全走皮肤类(.ze-gizmo*,02-atoms/spatial.css 用 var(--ze-*);L2 组件原子)。
 */

import type { ReactNode } from 'react'
import { gizmoHandlePoints, imageToScreen } from './spatialMath'
import type { Geometry, GizmoConstraints, HandleId, Viewport } from './spatialTypes'

export type TransformGizmoProps = {
  frame: Geometry
  viewport: Viewport
  constraints: GizmoConstraints
  activeHandle?: HandleId | null
}

const EDGES: HandleId[] = ['n', 'e', 's', 'w']
const CORNERS: HandleId[] = ['nw', 'ne', 'se', 'sw']
const HS = 4  // 把手半边(屏幕 px)

export function TransformGizmo({ frame, viewport, constraints, activeHandle }: TransformGizmoProps): ReactNode {
  const armImg = 26 / viewport.scale
  const ptsImg = gizmoHandlePoints(frame, armImg)
  const s = (h: HandleId) => imageToScreen(ptsImg[h], viewport)
  const nw = s('nw'), ne = s('ne'), se = s('se'), sw = s('sw'), n = s('n'), rot = s('rotate')

  const handles: HandleId[] = constraints.rotationEnabled
    ? [...CORNERS, ...EDGES, 'rotate']
    : [...CORNERS, ...EDGES]

  return (
    <g className="ze-gizmo">
      <polygon
        className="ze-gizmo__frame"
        points={`${nw.x},${nw.y} ${ne.x},${ne.y} ${se.x},${se.y} ${sw.x},${sw.y}`}
      />
      {constraints.rotationEnabled && (
        <line className="ze-gizmo__arm" x1={n.x} y1={n.y} x2={rot.x} y2={rot.y} />
      )}
      {handles.map((h) => {
        const p = s(h)
        if (h === 'rotate') {
          return <circle key={h} className="ze-gizmo__handle" data-handle="rotate"
            data-active={activeHandle === h ? 'true' : undefined} cx={p.x} cy={p.y} r={HS + 1} />
        }
        return <rect key={h} className="ze-gizmo__handle" data-handle={h}
          data-active={activeHandle === h ? 'true' : undefined}
          x={p.x - HS} y={p.y - HS} width={HS * 2} height={HS * 2} />
      })}
    </g>
  )
}
