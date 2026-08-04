import { describe, expect, it } from 'vitest'
import {
  keyframeMarkerPresentation,
  keyframeAt,
  keyframesFor,
  resolveGuideGeometry,
  type GuideGeometry,
} from './guideKeyframes'

const BASE: GuideGeometry = { x: 9, y: 8, w: 7, h: 6 }
const FRAMES = [
  { id: 'kf-a', parentId: 'ann-1', time: 0, bboxX: 0, bboxY: 10, bboxW: 100, bboxH: 50 },
  { id: 'kf-b', parentId: 'ann-1', time: 2, bboxX: 200, bboxY: 110, bboxW: 140, bboxH: 70 },
]

describe('guide geometry keyframes', () => {
  it('uses a compact, interactive marker instead of a large invisible button', () => {
    expect(keyframeMarkerPresentation()).toEqual({
      size: 'lg', variant: 'ghost', zIndex: 3,
      hitSize: 'var(--ze-space-4)', pointerEvents: 'auto',
    })
  })

  it('uses the original geometry when there are no keyframes', () => {
    expect(resolveGuideGeometry(BASE, [], 1)).toEqual(BASE)
  })

  it('interpolates position and size linearly between two keyframes', () => {
    expect(resolveGuideGeometry(BASE, FRAMES, 1)).toEqual({
      x: 100, y: 60, w: 120, h: 60,
    })
  })

  it('holds the first and last keyframe outside their range', () => {
    expect(resolveGuideGeometry(BASE, FRAMES, -1)).toEqual({
      x: 0, y: 10, w: 100, h: 50,
    })
    expect(resolveGuideGeometry(BASE, FRAMES, 4)).toEqual({
      x: 200, y: 110, w: 140, h: 70,
    })
  })

  it('filters by guide and finds an exact frame using half-frame tolerance', () => {
    const mixed = [...FRAMES, { ...FRAMES[0], id: 'other', parentId: 'ann-2' }]
    expect(keyframesFor(mixed, 'ann-1').map((item) => item.id)).toEqual(['kf-a', 'kf-b'])
    expect(keyframeAt(FRAMES, 2.01, 30)?.id).toBe('kf-b')
    expect(keyframeAt(FRAMES, 2.02, 30)).toBeNull()
  })
})
