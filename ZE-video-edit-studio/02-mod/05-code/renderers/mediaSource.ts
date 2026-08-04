export type MediaSource = {
  /** Stable media identity. Short-lived access tokens are deliberately excluded. */
  key: string
  url: string
}

/**
 * Keep an already loaded video attached when only its short-lived access ticket
 * rotates. Replacing `video.src` for the same bytes makes browsers tear down the
 * decoder and briefly paint an empty frame.
 *
 * A later `error` event may explicitly promote the newest candidate URL.
 */
export function chooseStableMediaSource(
  current: MediaSource,
  candidate: MediaSource,
): MediaSource {
  if (!candidate.key) return candidate
  if (current.key !== candidate.key) return candidate
  if (!current.url && candidate.url) return candidate
  return current
}
