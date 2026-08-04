export type GuideGeometry = { x: number; y: number; w: number; h: number }

export type GuideKeyframe = {
  id?: string
  parentId?: string
  time?: number
  bboxX?: number
  bboxY?: number
  bboxW?: number
  bboxH?: number
  data?: Record<string, unknown>
}

/**
 * Timeline markers sit on top of their guide clip.  Keeping this small visual
 * contract next to the keyframe data prevents the selected clip/its handles
 * from visually swallowing the marker again.
 */
export function keyframeMarkerPresentation(): {
  size: 'lg'
  variant: 'ghost'
  zIndex: number
  hitSize: string
  pointerEvents: 'auto'
} {
  return {
    size: 'lg',
    // 时间轴上只留一个放大的菱形；命中区与它一致，而不是默认的大按钮块。
    variant: 'ghost',
    zIndex: 3,
    hitSize: 'var(--ze-space-4)',
    pointerEvents: 'auto',
  }
}

const num = (value: unknown, fallback = 0): number => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

const field = (frame: GuideKeyframe, name: string): unknown => (
  frame.data?.[name] ?? frame[name as keyof GuideKeyframe]
)

const geometryOf = (frame: GuideKeyframe): GuideGeometry => ({
  x: num(field(frame, 'bboxX')),
  y: num(field(frame, 'bboxY')),
  w: num(field(frame, 'bboxW')),
  h: num(field(frame, 'bboxH')),
})

export function keyframesFor(
  frames: GuideKeyframe[],
  annotationId: string,
): GuideKeyframe[] {
  return frames
    .filter((frame) => String(field(frame, 'parentId') || '') === annotationId)
    .sort((a, b) => num(field(a, 'time')) - num(field(b, 'time')))
}

export function keyframeAt(
  frames: GuideKeyframe[],
  localTime: number,
  fps: number,
): GuideKeyframe | null {
  const tolerance = 0.5 / Math.max(1, fps)
  return frames.find((frame) => Math.abs(num(field(frame, 'time')) - localTime) <= tolerance)
    ?? null
}

export function resolveGuideGeometry(
  base: GuideGeometry,
  frames: GuideKeyframe[],
  localTime: number,
): GuideGeometry {
  if (!frames.length) return { ...base }
  const ordered = [...frames].sort(
    (a, b) => num(field(a, 'time')) - num(field(b, 'time')),
  )
  if (localTime <= num(field(ordered[0], 'time'))) return geometryOf(ordered[0])
  const last = ordered[ordered.length - 1]
  if (localTime >= num(field(last, 'time'))) return geometryOf(last)

  for (let index = 1; index < ordered.length; index += 1) {
    const right = ordered[index]
    const rightTime = num(field(right, 'time'))
    if (localTime > rightTime) continue
    const left = ordered[index - 1]
    const leftTime = num(field(left, 'time'))
    const span = Math.max(Number.EPSILON, rightTime - leftTime)
    const progress = (localTime - leftTime) / span
    const a = geometryOf(left)
    const b = geometryOf(right)
    return {
      x: a.x + (b.x - a.x) * progress,
      y: a.y + (b.y - a.y) * progress,
      w: a.w + (b.w - a.w) * progress,
      h: a.h + (b.h - a.h) * progress,
    }
  }
  return geometryOf(last)
}
