import { describe, expect, it } from 'vitest'
import {
  GUIDE_COLORS,
  arrowPress,
  arrowRelease,
  arrowMove,
  guideColorControlMode,
  isGuideColorEditable,
} from './guideTools'

describe('guide colors', () => {
  it('offers exactly black, white and red', () => {
    expect(GUIDE_COLORS).toEqual([
      { id: 'black', value: '#000000', labelKey: 'mod.color.black', glyph: '⬛' },
      { id: 'white', value: '#ffffff', labelKey: 'mod.color.white', glyph: '⬜' },
      { id: 'red', value: '#ff3b30', labelKey: 'mod.color.red', glyph: '🟥' },
    ])
  })

  it('can be changed after selecting a box or arrow, but not text or image', () => {
    expect(isGuideColorEditable('box')).toBe(true)
    expect(isGuideColorEditable('arrow')).toBe(true)
    expect(isGuideColorEditable('text')).toBe(false)
    expect(isGuideColorEditable('image')).toBe(false)
  })

  it('uses the same top control while drawing or editing an existing guide', () => {
    expect(guideColorControlMode('rect', null)).toBe('drawing')
    expect(guideColorControlMode('arrow', null)).toBe('drawing')
    expect(guideColorControlMode('select', 'box')).toBe('selection')
    expect(guideColorControlMode('select', 'arrow')).toBe('selection')
    expect(guideColorControlMode('select', 'text')).toBeNull()
    expect(guideColorControlMode('select', null)).toBeNull()
  })
})

describe('press-drag-release arrow gesture', () => {
  it('uses pointer down as the tail and pointer release as the head', () => {
    const pressed = arrowPress({ x: 10, y: 20 }, 1.25)
    expect(pressed).toMatchObject({
      from: { x: 10, y: 20 },
      to: { x: 10, y: 20 },
      lo: 1.25,
      hi: 1.25,
    })

    const moved = arrowMove(pressed, { x: 40, y: 50 })
    expect(moved.to).toEqual({ x: 40, y: 50 })

    const committed = arrowRelease(moved, { x: 70, y: 80 }, 1.25, 0.6)
    expect(committed).toMatchObject({
      from: { x: 10, y: 20 },
      to: { x: 70, y: 80 },
    })
  })

  it('does not create an arrow from a plain click without a drag', () => {
    const pressed = arrowPress({ x: 10, y: 20 }, 1.25)
    expect(arrowRelease(pressed, { x: 10, y: 20 }, 1.25, 0.6)).toBeNull()
  })

  it('can begin another arrow immediately after one is released', () => {
    const first = arrowPress({ x: 5, y: 5 }, 0)
    const completed = arrowRelease(first, { x: 25, y: 25 }, 0, 0.6)
    const next = arrowPress({ x: 60, y: 60 }, 0)

    expect(completed).not.toBeNull()
    expect(next.from).toEqual({ x: 60, y: 60 })
  })
})
