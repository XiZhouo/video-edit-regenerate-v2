import { describe, expect, it } from 'vitest'
import { deletionForKey } from './selectionDeletion'

const annotations = [{ id: 'ann-image', version: 3 }]
const clips = [{ id: 'clip-1', version: 7 }]

describe('keyboard deletion', () => {
  it('uses the same annotation removal command as the timeline', () => {
    expect(deletionForKey(
      { key: 'Delete', target: null },
      { type: 'annotation', id: 'ann-image' },
      annotations,
      clips,
    )).toEqual({ commandId: 'annotation.remove', payload: { id: 'ann-image' } })
  })

  it('supports the Backspace key used as Delete on Mac keyboards', () => {
    expect(deletionForKey(
      { key: 'Backspace', target: null },
      { type: 'clip', id: 'clip-1' },
      annotations,
      clips,
    )).toEqual({
      commandId: 'entity.delete',
      payload: { type: 'clip', id: 'clip-1', expected_version: 7 },
    })
  })

  it('does not delete while the user is typing', () => {
    expect(deletionForKey(
      { key: 'Backspace', target: { tagName: 'INPUT' } },
      { type: 'annotation', id: 'ann-image' },
      annotations,
      clips,
    )).toBeNull()
  })
})
