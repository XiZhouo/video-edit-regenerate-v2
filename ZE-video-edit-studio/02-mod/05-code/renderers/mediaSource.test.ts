import { describe, expect, it } from 'vitest'
import { chooseStableMediaSource } from './mediaSource'

describe('stable video source', () => {
  it('keeps the current URL when only the short-lived ticket changes', () => {
    expect(chooseStableMediaSource(
      { key: 'same-video', url: '/video?ticket=old' },
      { key: 'same-video', url: '/video?ticket=new' },
    )).toEqual({ key: 'same-video', url: '/video?ticket=old' })
  })

  it('accepts the first usable URL and switches when the actual media changes', () => {
    expect(chooseStableMediaSource(
      { key: 'same-video', url: '' },
      { key: 'same-video', url: '/video?ticket=ready' },
    )).toEqual({ key: 'same-video', url: '/video?ticket=ready' })

    expect(chooseStableMediaSource(
      { key: 'old-video', url: '/old' },
      { key: 'new-video', url: '/new' },
    )).toEqual({ key: 'new-video', url: '/new' })
  })
})
