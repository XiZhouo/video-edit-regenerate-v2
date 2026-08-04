/**
 * spatialMath — 2D 空间编辑器纯几何(框架级,无 React;正确性核心,有 fixture 测试)
 *
 * 关键设计:缩放在「锚点固定」下做 —— 拖某把手时,对角把手(锚点)在图像空间保持不动,
 * 旋转下也成立(把手位置 = 中心 + sx·w/2·ux + sy·h/2·uy,ux/uy 是旋转后的 box 局部轴)。
 * 故无需先把指针转进局部坐标再转回,直接用 box 局部轴投影,数值稳定、可单测。
 */

import type { Box, Geometry, GizmoConstraints, HandleId, Vec2, Viewport } from './spatialTypes'

const RAD = Math.PI / 180

export function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v
}

export function screenToImage(p: Vec2, vp: Viewport): Vec2 {
  return { x: (p.x - vp.tx) / vp.scale, y: (p.y - vp.ty) / vp.scale }
}
export function imageToScreen(p: Vec2, vp: Viewport): Vec2 {
  return { x: p.x * vp.scale + vp.tx, y: p.y * vp.scale + vp.ty }
}

/** wheel zoom centered on a container-local point: keep the image point under the cursor fixed. */
export function zoomAt(vp: Viewport, point: Vec2, factor: number, minScale: number, maxScale: number): Viewport {
  const newScale = clamp(vp.scale * factor, minScale, maxScale)
  const imgPt = screenToImage(point, vp)
  return { scale: newScale, tx: point.x - imgPt.x * newScale, ty: point.y - imgPt.y * newScale }
}

/** fit-contain: 内容(图像 px)居中铺进容器(屏幕 px),留 4% 边。 */
export function fitViewport(content: { w: number; h: number }, container: { w: number; h: number }): Viewport {
  if (content.w <= 0 || content.h <= 0) return { scale: 1, tx: 0, ty: 0 }
  const scale = Math.min(container.w / content.w, container.h / content.h) * 0.96 || 1
  return {
    scale,
    tx: (container.w - content.w * scale) / 2,
    ty: (container.h - content.h * scale) / 2,
  }
}

/** box 局部 +x / +y 轴在图像空间的单位向量(随 rotation 转)。 */
function axes(rotationDeg: number): { ux: Vec2; uy: Vec2 } {
  const r = rotationDeg * RAD
  const c = Math.cos(r), s = Math.sin(r)
  return { ux: { x: c, y: s }, uy: { x: -s, y: c } }
}

function centerOf(g: Geometry): Vec2 {
  return { x: g.x + g.w / 2, y: g.y + g.h / 2 }
}

/** 把手相对中心的符号 (sx, sy);edge 某轴为 0;rotate 不在此。 */
const HANDLE_SIGN: Record<Exclude<HandleId, 'rotate'>, [number, number]> = {
  nw: [-1, -1], n: [0, -1], ne: [1, -1], e: [1, 0],
  se: [1, 1], s: [0, 1], sw: [-1, 1], w: [-1, 0],
}

/** 8 把手 + 旋转臂尖在图像空间的位置(rotation 已应用)。 */
export function gizmoHandlePoints(g: Geometry, armLenImg: number): Record<HandleId, Vec2> {
  const { ux, uy } = axes(g.rotation ?? 0)
  const c = centerOf(g)
  const at = (sx: number, sy: number): Vec2 => ({
    x: c.x + sx * (g.w / 2) * ux.x + sy * (g.h / 2) * uy.x,
    y: c.y + sx * (g.w / 2) * ux.y + sy * (g.h / 2) * uy.y,
  })
  const out = {} as Record<HandleId, Vec2>
  for (const h of Object.keys(HANDLE_SIGN) as (keyof typeof HANDLE_SIGN)[]) {
    const [sx, sy] = HANDLE_SIGN[h]
    out[h] = at(sx, sy)
  }
  // 旋转臂尖:上边中点再往外 armLenImg
  out.rotate = {
    x: c.x + 0 * ux.x + (-(g.h / 2) - armLenImg) * uy.x,
    y: c.y + 0 * ux.y + (-(g.h / 2) - armLenImg) * uy.y,
  }
  return out
}

/** 反向 z(后画=上层)命中测试:返回最上层包含 imgPt 的 box id。 */
export function hitTest(imgPt: Vec2, boxes: { id: string; geom: Geometry }[]): string | null {
  for (let i = boxes.length - 1; i >= 0; i--) {
    const g = boxes[i].geom
    const { ux, uy } = axes(g.rotation ?? 0)
    const c = centerOf(g)
    const rel = { x: imgPt.x - c.x, y: imgPt.y - c.y }
    const lx = rel.x * ux.x + rel.y * ux.y
    const ly = rel.x * uy.x + rel.y * uy.y
    if (Math.abs(lx) <= g.w / 2 && Math.abs(ly) <= g.h / 2) return boxes[i].id
  }
  return null
}

/** 中心拖动:平移(图像空间 delta;旋转无关)。 */
export function applyMove(start: Geometry, deltaImg: Vec2): Geometry {
  return { ...start, x: start.x + deltaImg.x, y: start.y + deltaImg.y }
}

/** 旋转把手 → 绕中心角度。snapDeg>0 时吸附(如 Shift 15°)。 */
export function applyRotate(start: Geometry, pointerImg: Vec2, snapDeg = 0): Geometry {
  const c = centerOf(start)
  const ang = Math.atan2(pointerImg.y - c.y, pointerImg.x - c.x) / RAD + 90  // 臂在 -90° 方向
  let deg = ((ang % 360) + 360) % 360
  if (snapDeg > 0) deg = Math.round(deg / snapDeg) * snapDeg
  return { ...start, rotation: deg }
}

/** 把手拖拽 → 新几何(锚点固定;角点缩放/边缩放/Shift 锁比例;min/max 钳)。 */
export function applyHandleDrag(args: {
  start: Geometry
  handle: Exclude<HandleId, 'rotate'>
  pointerImg: Vec2
  shiftAspectLock: boolean
  constraints: GizmoConstraints
}): Geometry {
  const { start, handle, pointerImg, shiftAspectLock, constraints: c } = args
  const [sx, sy] = HANDLE_SIGN[handle]
  const { ux, uy } = axes(start.rotation ?? 0)
  const w0 = start.w, h0 = start.h
  const C = centerOf(start)
  // 锚点 = 对角把手当前图像位置(固定不动)
  const A: Vec2 = {
    x: C.x - sx * (w0 / 2) * ux.x - sy * (h0 / 2) * uy.x,
    y: C.y - sx * (w0 / 2) * ux.y - sy * (h0 / 2) * uy.y,
  }
  const dp = { x: pointerImg.x - A.x, y: pointerImg.y - A.y }
  const alongX = dp.x * ux.x + dp.y * ux.y
  const alongY = dp.x * uy.x + dp.y * uy.y
  let newW = sx !== 0 ? alongX * sx : w0
  let newH = sy !== 0 ? alongY * sy : h0

  const isCorner = sx !== 0 && sy !== 0
  if (isCorner && (shiftAspectLock || c.aspect)) {
    const aspect = c.aspect || (h0 !== 0 ? w0 / h0 : 1)
    // 取相对变化更大的轴作主动轴
    if (Math.abs(newW / (w0 || 1)) >= Math.abs(newH / (h0 || 1))) newH = newW / aspect
    else newW = newH * aspect
  }
  newW = clamp(newW, c.minW, c.maxW ?? Infinity)
  newH = clamp(newH, c.minH, c.maxH ?? Infinity)

  const ncx = A.x + sx * (newW / 2) * ux.x + sy * (newH / 2) * uy.x
  const ncy = A.y + sx * (newW / 2) * ux.y + sy * (newH / 2) * uy.y
  return { x: ncx - newW / 2, y: ncy - newH / 2, w: newW, h: newH, rotation: start.rotation }
}

export function clampGeometry(g: Geometry, c: GizmoConstraints): Geometry {
  return { ...g, w: clamp(g.w, c.minW, c.maxW ?? Infinity), h: clamp(g.h, c.minH, c.maxH ?? Infinity) }
}

/** 从两个图像空间点算 marquee 矩形(归一化)。 */
export function boxFromPoints(a: Vec2, b: Vec2): Box {
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) }
}

export const OPPOSITE: Record<Exclude<HandleId, 'rotate'>, Exclude<HandleId, 'rotate'>> = {
  nw: 'se', n: 's', ne: 'sw', e: 'w', se: 'nw', s: 'n', sw: 'ne', w: 'e',
}
