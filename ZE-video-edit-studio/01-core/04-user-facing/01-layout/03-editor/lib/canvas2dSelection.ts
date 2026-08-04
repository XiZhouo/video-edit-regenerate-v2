/**
 * canvas2dSelection — lasso/polygon 选区交互的纯几何逻辑(无 DOM,node 可测)。
 *
 * Canvas2dLayerStage 的手绘 lasso(单次拖拽采点)与多边形 polygon(多次点击落顶点)共用这几个
 * 判定:采点稀释、可闭合环判定、点回起点闭合判定。实际像素填充由组件用 canvas ctx.fill() 完成
 * (浏览器栅格化),这里只管「点集怎么收、何时能闭合」的确定性逻辑。
 */

export type SelPoint = { x: number; y: number }

/** 手绘采点稀释:新点离上一点 < minDist 时丢弃(返回原数组引用),否则并入(返回新数组,不可变)。 */
export function appendFreehandPoint(points: SelPoint[], p: SelPoint, minDist: number): SelPoint[] {
  const last = points[points.length - 1]
  if (last && Math.hypot(p.x - last.x, p.y - last.y) < minDist) return points
  return [...points, p]
}

/** 能闭合成面的环:至少 3 个顶点(填充才有意义)。 */
export function isClosableRing(points: SelPoint[]): boolean {
  return points.length >= 3
}

/** 两点是否在阈值内(多边形「点回起点 → 闭合」判定;阈值由调用方按缩放折算到世界坐标)。 */
export function isNearPoint(a: SelPoint, b: SelPoint, threshold: number): boolean {
  return Math.hypot(a.x - b.x, a.y - b.y) <= threshold
}
