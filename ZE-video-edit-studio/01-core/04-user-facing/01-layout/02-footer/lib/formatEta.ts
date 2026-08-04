/**
 * formatEta — 把剩余秒数格式化成紧凑可读串(纯逻辑,ASCII,便于单测)。
 *   < 60s  -> "45s"
 *   < 1h   -> "3m" / "3m20s"
 *   >= 1h  -> "1h" / "1h2m"
 */
export function formatEta(sec: number): string {
  const s = Math.max(0, Math.round(sec))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) {
    const rs = s % 60
    return rs ? `${m}m${rs}s` : `${m}m`
  }
  const h = Math.floor(m / 60)
  const rm = m % 60
  return rm ? `${h}h${rm}m` : `${h}h`
}
