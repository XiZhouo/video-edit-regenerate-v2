/**
 * useSpatialPointer — 2D 空间编辑器指针状态机(框架级)
 *
 * 决策:中键=pan;选中件 gizmo 把手命中(屏幕距离)=scale/rotate;add 模式空白拖=marquee 新增;
 * 命中 box=选中并 move;空白=取消选。setPointerCapture 让出界拖拽继续。变换实时进本地 preview,
 * 默认 pointerup 才 commit(领域写走 dispatch)。数学全在 spatialMath(纯、已测)。
 */

import { useCallback, useRef, useState } from 'react'
import {
  applyHandleDrag, applyMove, applyRotate, boxFromPoints, gizmoHandlePoints,
  hitTest, imageToScreen, screenToImage,
} from './spatialMath'
import type { Box, Geometry, GizmoConstraints, HandleId, Vec2, Viewport } from './spatialTypes'
import type { UseViewport } from './useViewport'

export type PointerKind = 'idle' | 'move' | 'scale-corner' | 'scale-edge' | 'rotate' | 'marquee-add' | 'pan'

const HANDLE_HIT_PX = 11
const ARM_SCREEN = 26
/** 按下→抬起位移不超过此值视作"单击"(非拖动);用于已选中件再击取消选中(F1)。 */
const CLICK_DESELECT_PX = 3
const CORNERS = new Set<HandleId>(['nw', 'ne', 'se', 'sw'])

export type UseSpatialPointerOpts = {
  containerRef: React.RefObject<HTMLElement | null>
  viewport: UseViewport
  imageSize?: { w: number; h: number }
  boxes: { id: string; geom: Geometry }[]
  selectedId: string | null
  constraints: GizmoConstraints
  mode: 'select' | 'add'
  minAddSize: number
  addEnabled: boolean
  onSelect: (id: string | null) => void
  onPreview: (id: string, geom: Geometry) => void
  onCommitGeometry: (id: string, geom: Geometry) => void
  onCommitAdd: (box: Box) => void
}

export type SpatialPointerBindings = {
  onPointerDown: React.PointerEventHandler
  onPointerMove: React.PointerEventHandler
  onPointerUp: React.PointerEventHandler
  onWheel: React.WheelEventHandler
  onDoubleClick: React.MouseEventHandler
}

export type UseSpatialPointer = {
  bind: SpatialPointerBindings
  kind: PointerKind
  preview: Geometry | null
  marquee: Box | null
}

type Drag = {
  kind: PointerKind
  id: string | null
  handle: HandleId | null
  startGeom: Geometry | null
  downImg: Vec2
  lastClient: Vec2
  /** 按下时命中的 box 已是选中件(pointerup 判"纯单击"则取消选中,F1)。 */
  downOnSelected?: boolean
}

export function useSpatialPointer(o: UseSpatialPointerOpts): UseSpatialPointer {
  const [kind, setKind] = useState<PointerKind>('idle')
  const [preview, setPreview] = useState<Geometry | null>(null)
  const [marquee, setMarquee] = useState<Box | null>(null)
  const drag = useRef<Drag | null>(null)

  const containerPoint = useCallback((e: { clientX: number; clientY: number }): Vec2 => {
    const r = o.containerRef.current?.getBoundingClientRect()
    return { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) }
  }, [o.containerRef])

  const toImg = useCallback((e: { clientX: number; clientY: number }, vp: Viewport): Vec2 =>
    screenToImage(containerPoint(e), vp), [containerPoint])

  /** 命中选中件的 gizmo 把手(屏幕距离),返回 handle 或 null。 */
  const hitHandle = useCallback((cp: Vec2, vp: Viewport): HandleId | null => {
    if (!o.selectedId) return null
    const sel = o.boxes.find((b) => b.id === o.selectedId)
    if (!sel) return null
    const armImg = ARM_SCREEN / vp.scale
    const pts = gizmoHandlePoints(sel.geom, armImg)
    const order: HandleId[] = o.constraints.rotationEnabled
      ? ['rotate', 'nw', 'ne', 'se', 'sw', 'n', 'e', 's', 'w']
      : ['nw', 'ne', 'se', 'sw', 'n', 'e', 's', 'w']
    for (const h of order) {
      const sp = imageToScreen(pts[h], vp)
      if (Math.hypot(sp.x - cp.x, sp.y - cp.y) <= HANDLE_HIT_PX) return h
    }
    return null
  }, [o.selectedId, o.boxes, o.constraints.rotationEnabled])

  const onPointerDown = useCallback<React.PointerEventHandler>((e) => {
    const vp = o.viewport.viewportRef.current
    const cp = containerPoint(e)
    const img = screenToImage(cp, vp)
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
    const base: Drag = { kind: 'idle', id: null, handle: null, startGeom: null, downImg: img, lastClient: { x: e.clientX, y: e.clientY } }

    if (e.button === 1) { drag.current = { ...base, kind: 'pan' }; setKind('pan'); return }

    // 1) gizmo 把手
    const h = hitHandle(cp, vp)
    if (h) {
      const sel = o.boxes.find((b) => b.id === o.selectedId)!
      const k: PointerKind = h === 'rotate' ? 'rotate' : CORNERS.has(h) ? 'scale-corner' : 'scale-edge'
      drag.current = { ...base, kind: k, id: sel.id, handle: h, startGeom: sel.geom }
      setKind(k); setPreview(sel.geom); return
    }

    // 2) add 模式空白 → marquee
    if (o.mode === 'add' && o.addEnabled) {
      drag.current = { ...base, kind: 'marquee-add' }
      setKind('marquee-add'); setMarquee({ x: img.x, y: img.y, w: 0, h: 0 }); return
    }

    // 3) 命中 box → 选中 + move(已选中件记 downOnSelected,pointerup 判纯单击则取消选中)
    const hitId = hitTest(img, o.boxes)
    if (hitId) {
      const downOnSelected = hitId === o.selectedId
      o.onSelect(hitId)
      const g = o.boxes.find((b) => b.id === hitId)!.geom
      drag.current = { ...base, kind: 'move', id: hitId, startGeom: g, downOnSelected }
      setKind('move'); setPreview(g); return
    }

    // 4) 空白 → 取消选
    o.onSelect(null)
    drag.current = { ...base, kind: 'idle' }
  }, [o, containerPoint, hitHandle])

  const onPointerMove = useCallback<React.PointerEventHandler>((e) => {
    const d = drag.current
    if (!d || d.kind === 'idle') return
    const vp = o.viewport.viewportRef.current
    if (d.kind === 'pan') {
      o.viewport.panBy({ x: e.clientX - d.lastClient.x, y: e.clientY - d.lastClient.y })
      d.lastClient = { x: e.clientX, y: e.clientY }
      return
    }
    const img = toImg(e, vp)
    if (d.kind === 'marquee-add') { setMarquee(boxFromPoints(d.downImg, img)); return }
    if (!d.startGeom || !d.id) return
    let g: Geometry
    if (d.kind === 'move') g = applyMove(d.startGeom, { x: img.x - d.downImg.x, y: img.y - d.downImg.y })
    else if (d.kind === 'rotate') g = applyRotate(d.startGeom, img, e.shiftKey ? 15 : 0)
    else g = applyHandleDrag({ start: d.startGeom, handle: d.handle as Exclude<HandleId, 'rotate'>, pointerImg: img, shiftAspectLock: e.shiftKey, constraints: o.constraints })
    setPreview(g)
    o.onPreview(d.id, g)
  }, [o, toImg])

  const onPointerUp = useCallback<React.PointerEventHandler>((e) => {
    const d = drag.current
    drag.current = null
    setKind('idle')
    ;(e.target as Element).releasePointerCapture?.(e.pointerId)
    if (!d) return
    if (d.kind === 'marquee-add' && marquee) {
      if (marquee.w >= o.minAddSize && marquee.h >= o.minAddSize) o.onCommitAdd(marquee)
      setMarquee(null); return
    }
    // 纯单击(未拖动)已选中件 → 再次单击取消选中(F1);不 commit 原地几何
    if (d.kind === 'move' && d.downOnSelected
      && Math.hypot(e.clientX - d.lastClient.x, e.clientY - d.lastClient.y) <= CLICK_DESELECT_PX) {
      o.onSelect(null)
      setPreview(null)
      return
    }
    if (d.id && preview && (d.kind === 'move' || d.kind === 'scale-corner' || d.kind === 'scale-edge' || d.kind === 'rotate')) {
      o.onCommitGeometry(d.id, preview)
    }
    setPreview(null)
  }, [o, marquee, preview])

  const onWheel = useCallback<React.WheelEventHandler>((e) => {
    o.viewport.zoomAt(containerPoint(e), e.deltaY)
  }, [o.viewport, containerPoint])

  const onDoubleClick = useCallback<React.MouseEventHandler>(() => {
    const el = o.containerRef.current
    if (!el || !o.imageSize) return
    o.viewport.fit(o.imageSize, { w: el.clientWidth, h: el.clientHeight })
  }, [o.viewport, o.containerRef, o.imageSize])

  return { bind: { onPointerDown, onPointerMove, onPointerUp, onWheel, onDoubleClick }, kind, preview, marquee }
}
