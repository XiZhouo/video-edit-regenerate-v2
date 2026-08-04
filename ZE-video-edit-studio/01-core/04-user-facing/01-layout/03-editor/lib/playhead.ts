/**
 * playhead — 共享时间游标(C0)双向同步的纯逻辑(防回环)。
 *
 * 媒体类编辑器既要把自身播放/scrub 的时间「提升」为共享 currentTime(向外写),
 * 又要在别的面板改了 currentTime 时把媒体元素 seek 过去(向内读)。两个方向直连会抖动:
 *   timeUpdate → setCurrentTime → props.currentTime 变 → seek → timeUpdate → …
 * 用「仅当与当前播放头差值超过阈值才 seek」打断回环 —— 微小回灌(同一帧的来回)被吸收,
 * 只有真实的外部跳转(用户在另一个面板拖了 playhead)才触发 seek。纯函数,与 React 无关、可单测。
 */

/** 默认抖动阈值(秒):≈ 30fps 下 1 帧多一点(也覆盖 60fps 下约 2 帧)。
 *  HTMLMediaElement 的 timeUpdate 节拍粗(~4Hz),阈值取得比单帧稍宽以稳妥吸收回灌,
 *  又远小于人眼可感的 scrub 跳变,故不会吞掉真实外部跳转。 */
export const DEFAULT_SEEK_EPSILON = 0.04

/**
 * 该不该把媒体元素 seek 到外部 currentTime?
 *   - target/actual 任一非有限(NaN/Infinity) → 不 seek(无效输入,不动元素)
 *   - |target - actual| > epsilon → seek(真实外部跳转)
 *   - 否则 → 不 seek(回灌 / 已对齐,避免 timeUpdate↔seek 抖动)
 *
 * @param target  外部共享 currentTime(秒)
 * @param actual  媒体元素当前 currentTime(秒)
 * @param epsilon 抖动阈值(秒);缺省 DEFAULT_SEEK_EPSILON
 */
export function shouldSeek(target: number, actual: number, epsilon: number = DEFAULT_SEEK_EPSILON): boolean {
  if (!Number.isFinite(target) || !Number.isFinite(actual)) return false
  return Math.abs(target - actual) > epsilon
}

/**
 * 共享 currentTime 的规范单位是「秒」;timeline 用抽象「unit」。两者经 unitsPerSecond 换算
 * (unit=帧时即 fps,unit=秒时即 1)。脏配置(<=0 / 非有限)一律退回 1,杜绝 0 除与 NaN 灌进
 * 几何量算(Math.max(…, playhead) 一旦吃到 NaN 会让整条时间线宽度坍塌)。纯函数,可单测。
 */
export function safeUnitsPerSecond(unitsPerSecond: number | undefined): number {
  return typeof unitsPerSecond === 'number' && Number.isFinite(unitsPerSecond) && unitsPerSecond > 0
    ? unitsPerSecond
    : 1
}

/** 共享 currentTime(秒)→ timeline 本地 unit。非有限输入 → 0。 */
export function secondsToUnits(seconds: number, unitsPerSecond: number | undefined): number {
  if (!Number.isFinite(seconds)) return 0
  return seconds * safeUnitsPerSecond(unitsPerSecond)
}

/** timeline 本地 unit → 共享 currentTime(秒)。非有限输入 → 0。 */
export function unitsToSeconds(units: number, unitsPerSecond: number | undefined): number {
  if (!Number.isFinite(units)) return 0
  return units / safeUnitsPerSecond(unitsPerSecond)
}
