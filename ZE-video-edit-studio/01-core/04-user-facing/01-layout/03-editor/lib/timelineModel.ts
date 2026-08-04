/**
 * timelineModel — 时间线纯几何/编辑量算(无 React,可单测)。
 *
 * 把 unit↔px、吸附、轨道命中、拖拽提交值从 TimelineZone 抽出,对齐 `canvas2dModel`/`playhead`
 * 的「纯逻辑分离」惯例(palmier-pro TimelineGeometry / MarketingCreatives timelineOps 同构)。
 * 片段占半开区间 [start, start+duration);右缘 end = start + duration —— 左把手 trim 时**固定 end**,
 * 改 start 与 duration 一次提交(原子)。所有越界/脏输入都收敛到合法值,绝不向几何量算泄漏 NaN。
 */

export const MIN_DURATION = 1
/** 默认半单位吸附(向后兼容旧 `round(u*2)/2`);view_state `snap` 可改栅格,0/负 = 关闭吸附。 */
export const DEFAULT_SNAP = 0.5
/** 未声明 `maxTracks` 时的有限 DOM/交互上限。 */
export const DEFAULT_MAX_TRACKS = 64
/** 即使 mod 配错极大 `maxTracks`，也不允许单个 timeline 制造无界 DOM。 */
export const MAX_TRACKS_HARD_LIMIT = 512

export const clampNum = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v))

/** 将声明的轨道容量收敛为安全正整数。缺失/非有限/非正数回退到有限默认值。 */
export function normalizeMaxTracks(value: unknown = DEFAULT_MAX_TRACKS): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 1) return DEFAULT_MAX_TRACKS
  return clampNum(Math.floor(value), 1, MAX_TRACKS_HARD_LIMIT)
}

/** 唯一轨号规范器：小数向下取整，负数/非数回到首轨，超上限数据收敛到末轨。
 * 这使历史脏数据仍可见、可选、可拖，不会因严格 lane equality 静默消失。 */
export function normalizeTrack(value: unknown, maxTracks: unknown = DEFAULT_MAX_TRACKS): number {
  const limit = normalizeMaxTracks(maxTracks)
  const integer = typeof value === 'number' && Number.isFinite(value) ? Math.floor(value) : 0
  return clampNum(integer, 0, limit - 1)
}

/** 吸附到 granularity 栅格(0.5=半单位 / 1=整单位 / 1/fps=帧)。granularity 非正/非有限 → 不吸附;u 非有限 → 0。 */
export function snapUnits(u: number, granularity: number = DEFAULT_SNAP): number {
  if (!Number.isFinite(u)) return 0
  if (!Number.isFinite(granularity) || granularity <= 0) return u
  return Math.round(u / granularity) * granularity
}

/** 像素 x(视口坐标)→ unit(相对内容左缘 contentLeft)。pxPerUnit 非正 → 0。 */
export function unitsFromX(clientX: number, contentLeft: number, pxPerUnit: number): number {
  if (!(pxPerUnit > 0)) return 0
  return (clientX - contentLeft) / pxPerUnit
}

/** 像素 y → 轨道索引(0..trackCount-1)。lanesHeight 非正 / trackCount<1 → 0。 */
export function trackFromY(clientY: number, lanesTop: number, lanesHeight: number, trackCount: number): number {
  if (!(lanesHeight > 0) || !Number.isFinite(trackCount) || trackCount < 1) return 0
  const count = normalizeMaxTracks(trackCount)
  return normalizeTrack(Math.floor((clientY - lanesTop) / (lanesHeight / count)), count)
}

/** move:水平拖动改 start,吸附后夹在 [0, totalUnits-min]。 */
export function moveStart(origStart: number, deltaUnits: number, totalUnits: number,
                          snap: number = DEFAULT_SNAP, min: number = MIN_DURATION): number {
  return clampNum(snapUnits(origStart + deltaUnits, snap), 0, totalUnits - min)
}

/** 右把手 resize:start 固定,改 duration(>=min)。 */
export function resizeDuration(origDuration: number, deltaUnits: number,
                               snap: number = DEFAULT_SNAP, min: number = MIN_DURATION): number {
  return Math.max(min, snapUnits(origDuration + deltaUnits, snap))
}

/** 左把手 trim:右缘 end=start+duration **固定**,改 start 与 duration。
 *  newStart 夹在 [0, end-min];newDuration = end - newStart(>=min)。返回两字段,调用方一次提交补丁。 */
export function trimStart(origStart: number, origDuration: number, deltaUnits: number,
                          snap: number = DEFAULT_SNAP, min: number = MIN_DURATION): { start: number; duration: number } {
  const end = origStart + origDuration
  const start = clampNum(snapUnits(origStart + deltaUnits, snap), 0, end - min)
  return { start, duration: end - start }
}

export type TimelineCreateRange = {
  start: number
  duration: number
  /** 两个原始端点分别经过 snap + clamp 后是否仍不同；只有 true 才能创建实体。 */
  valid: boolean
}

/** create:在空轨上按住拖拽拉出范围。
 *
 * anchor/cur 会先各自吸附并独立夹到 [0,totalUnits]，`valid` 只比较这两个最终端点。
 * 这条顺序很重要：同一吸附点内的抖动、或右边界外继续拖动，都不能被最小时长补齐误判为
 * 真拖拽。几何结果仍保证 duration>=min；调用方只能在 `valid=true` 时 dispatch addCommand。
 */
export function createRange(anchorUnits: number, curUnits: number, totalUnits: number,
                            snap: number = DEFAULT_SNAP, min: number = MIN_DURATION): TimelineCreateRange {
  const minimum = Number.isFinite(min) && min > 0 ? min : MIN_DURATION
  const total = Number.isFinite(totalUnits) ? Math.max(minimum, totalUnits) : minimum
  const anchor = clampNum(snapUnits(anchorUnits, snap), 0, total)
  const current = clampNum(snapUnits(curUnits, snap), 0, total)
  const start = clampNum(Math.min(anchor, current), 0, total - minimum)
  const end = clampNum(Math.max(anchor, current), start + minimum, total)
  return { start, duration: end - start, valid: anchor !== current }
}

/** 轨道行数。dynamic=true 时忽略 fixed，按数据中的最大占用轨渲染，未满容量时在末尾保留一条空轨；
 * 满 `maxTracks` 后不再增长。非 dynamic 优先使用 fixed，但 fixed/数据占用都不得突破安全容量。
 * 轨号的可见性由渲染层使用同一 `normalizeTrack(value, trackCount)` 保证。 */
export function laneCount(
  trackValues: readonly unknown[],
  opts: { dynamic?: boolean; fixed?: number; maxTracks?: number } = {},
): number {
  const maxTracks = normalizeMaxTracks(opts.maxTracks)
  const occupied = trackValues.length
    ? trackValues.reduce<number>((maxTrack, value) => {
        const track = normalizeTrack(value, maxTracks)
        return Math.max(maxTrack, track)
      }, 0) + 1
    : 0
  if (opts.dynamic === true) return Math.max(1, Math.min(maxTracks, occupied + (occupied < maxTracks ? 1 : 0)))
  const fixed = typeof opts.fixed === 'number' && Number.isFinite(opts.fixed)
    ? clampNum(Math.floor(opts.fixed), 1, maxTracks)
    : undefined
  return Math.max(1, Math.min(maxTracks, fixed ?? occupied))
}
