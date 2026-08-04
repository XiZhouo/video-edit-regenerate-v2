export type Pt = { x: number; y: number }
export type Box = { x: number; y: number; w: number; h: number }
export type Grip = 'draw' | 'move' | 'nw' | 'ne' | 'sw' | 'se' | 'clipMove' | 'clipScale'

export const clamp100 = (v: number): number => Math.max(0, Math.min(100, v))

/** 两点 → 框(画面百分比),并夹进画面内。 */
export function boxOf(from: Pt, to: Pt): Box {
  const x0 = clamp100(Math.min(from.x, to.x)), x1 = clamp100(Math.max(from.x, to.x))
  const y0 = clamp100(Math.min(from.y, to.y)), y1 = clamp100(Math.max(from.y, to.y))
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
}

/** 移动 / 拉角之后这个框在哪(画面百分比)。 */
export function movedBox(d: { grip: Grip; base?: Box; from: Pt; to: Pt }): Box {
  const b = d.base ?? { x: 0, y: 0, w: 0, h: 0 }
  const dx = d.to.x - d.from.x, dy = d.to.y - d.from.y
  if (d.grip === 'move') {
    const maxX = Math.max(0, 100 - b.w)
    const maxY = Math.max(0, 100 - b.h)
    return {
      x: Math.max(0, Math.min(maxX, b.x + dx)),
      y: Math.max(0, Math.min(maxY, b.y + dy)),
      w: b.w,
      h: b.h,
    }
  }
  const left = d.grip === 'nw' || d.grip === 'sw'
  const topSide = d.grip === 'nw' || d.grip === 'ne'
  const x0 = left ? b.x + dx : b.x
  const x1 = left ? b.x + b.w : b.x + b.w + dx
  const y0 = topSide ? b.y + dy : b.y
  const y1 = topSide ? b.y + b.h : b.y + b.h + dy
  return boxOf({ x: x0, y: y0 }, { x: x1, y: y1 })
}
