/** Gallery personal view state: persisted through the shared scoped view-state store. */

import type { ViewStateSliceCodec } from '@ze/core/04-user-facing/05-preferences'
import { asThumbSize, clampThumbScale, thumbScaleForSize } from './cardListModel'

export const GALLERY_VIEW_STATE_SLICE = 'gallery'
export const GALLERY_VIEW_STATE_VERSION = 1

export type GalleryViewState = {
  query: string
  pills: Record<string, string | null>
  sortId: string | null
  thumbSize: number
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function pills(value: unknown): Record<string, string | null> {
  const rec = record(value)
  if (!rec) return {}
  return Object.fromEntries(
    Object.entries(rec).filter((entry): entry is [string, string | null] => (
      typeof entry[1] === 'string' || entry[1] === null
    )),
  )
}

function thumbSize(value: unknown, fallback: number): number {
  const legacy = asThumbSize(value)
  return legacy ? thumbScaleForSize(legacy) : clampThumbScale(value, fallback)
}

export function galleryViewStateCodec(defaultThumbSize: number): ViewStateSliceCodec<GalleryViewState> {
  const fallback = clampThumbScale(defaultThumbSize)
  const decode = (value: unknown): GalleryViewState | null => {
    const rec = record(value)
    if (!rec) return null
    return {
      query: typeof rec.query === 'string' ? rec.query : '',
      pills: pills(rec.pills),
      sortId: typeof rec.sortId === 'string' ? rec.sortId : null,
      thumbSize: thumbSize(rec.thumbSize, fallback),
    }
  }
  return {
    version: GALLERY_VIEW_STATE_VERSION,
    decode,
    migrate: (value, fromVersion) => {
      if (fromVersion !== 0) return null
      const legacy = record(value)
      if (!legacy) return null
      return decode({
        query: legacy.query ?? legacy.search,
        pills: legacy.pills ?? legacy.filters,
        sortId: legacy.sortId ?? legacy.sort,
        thumbSize: legacy.thumbSize ?? legacy.thumbScale,
      })
    },
  }
}
