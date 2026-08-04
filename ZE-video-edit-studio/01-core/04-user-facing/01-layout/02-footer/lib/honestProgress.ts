/**
 * Converts pipeline progress into one honest UI/ARIA presentation.
 *
 * A finite producer value is determinate. An explicit completed status is also
 * authoritative and may resolve to 100%. Everything else stays indeterminate:
 * it must not acquire a made-up fill width or aria-valuenow.
 */
export type HonestProgressPresentation = {
  indeterminate: boolean
  percent: number | null
  ariaValueNow: number | undefined
  fillWidth: string | undefined
}

export function resolveHonestProgress(
  progress: unknown,
  completed: boolean,
): HonestProgressPresentation {
  const normalized = completed
    ? 1
    : typeof progress === 'number' && Number.isFinite(progress)
      ? Math.max(0, Math.min(1, progress))
      : null

  if (normalized === null) {
    return {
      indeterminate: true,
      percent: null,
      ariaValueNow: undefined,
      fillWidth: undefined,
    }
  }

  const percent = Math.round(normalized * 100)
  return {
    indeterminate: false,
    percent,
    ariaValueNow: percent,
    fillWidth: `${percent}%`,
  }
}
