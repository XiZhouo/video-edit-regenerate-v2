import { describe, expect, it } from 'vitest'
import { movedBox } from './canvasGeometry'

describe('moving a guide on the canvas', () => {
  it('keeps the whole guide reachable at the right and bottom edges', () => {
    expect(movedBox({
      grip: 'move',
      base: { x: 70, y: 80, w: 30, h: 20 },
      from: { x: 70, y: 80 },
      to: { x: 150, y: 160 },
    })).toEqual({ x: 70, y: 80, w: 30, h: 20 })
  })

  it('keeps the whole guide reachable at the left and top edges', () => {
    expect(movedBox({
      grip: 'move',
      base: { x: 20, y: 25, w: 30, h: 20 },
      from: { x: 20, y: 25 },
      to: { x: -100, y: -100 },
    })).toEqual({ x: 0, y: 0, w: 30, h: 20 })
  })
})
