/**
 * curveModel — keyframe-curve 编辑器纯量算(无 React,可单测)。
 *
 * 时间轴(x)复用 `timelineModel`(time↔px、吸附);本模块只管「值(value)↔ 纵向 px」、
 * 自适应值域、按时间排序。纵轴朝上(value 越大越靠上),故 y = height·(1 − (value−min)/(max−min))。
 * 所有退化/脏输入收敛到合法值,绝不向几何泄漏 NaN。
 */
import { clampNum } from './timelineModel'

export type ValueRange = { min: number; max: number }

/** 自适应值域:取数据 value 的 [min,max] + pad 余量;无数据→[0,1];退化(min==max)→中心 ±0.5。 */
export function autoValueRange(values: number[], pad = 0.1): ValueRange {
  const finite = values.filter((v) => Number.isFinite(v))
  if (finite.length === 0) return { min: 0, max: 1 }
  let min = Math.min(...finite)
  let max = Math.max(...finite)
  if (min === max) { min -= 0.5; max += 0.5 }
  const span = max - min
  return { min: min - span * pad, max: max + span * pad }
}

/** value → 纵向像素(0=顶 / height=底;value 越大越靠上)。range/height 退化时居中。 */
export function valueToY(value: number, range: ValueRange, height: number): number {
  const span = range.max - range.min
  if (!(span > 0) || !(height > 0) || !Number.isFinite(value)) return height > 0 ? height / 2 : 0
  return clampNum(height * (1 - (value - range.min) / span), 0, height)
}

/** 纵向像素 → value(valueToY 的逆;含 clamp 到值域)。 */
export function yToValue(y: number, range: ValueRange, height: number): number {
  const span = range.max - range.min
  if (!(height > 0)) return range.min
  const t = clampNum(1 - y / height, 0, 1)
  return range.min + t * span
}

/** 按时间升序排序(渲染折线用;稳定、不改原数组)。 */
export function sortByTime<T>(items: readonly T[], timeOf: (t: T) => number): T[] {
  return [...items].sort((a, b) => timeOf(a) - timeOf(b))
}
