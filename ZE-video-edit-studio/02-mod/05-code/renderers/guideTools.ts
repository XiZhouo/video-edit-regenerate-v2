export type GuideColor = '#000000' | '#ffffff' | '#ff3b30'

export const GUIDE_COLORS: {
  id: 'black' | 'white' | 'red'
  value: GuideColor
  labelKey: string
  glyph: string
}[] = [
  { id: 'black', value: '#000000', labelKey: 'mod.color.black', glyph: '⬛' },
  { id: 'white', value: '#ffffff', labelKey: 'mod.color.white', glyph: '⬜' },
  { id: 'red', value: '#ff3b30', labelKey: 'mod.color.red', glyph: '🟥' },
]

export const DEFAULT_GUIDE_COLOR: GuideColor = GUIDE_COLORS[2].value

export function isGuideColorEditable(kind: string): boolean {
  return kind === 'box' || kind === 'arrow'
}

export type GuideColorControlMode = 'drawing' | 'selection'

export function guideColorControlMode(
  tool: string,
  selectedKind: string | null,
): GuideColorControlMode | null {
  if (tool === 'rect' || tool === 'arrow') return 'drawing'
  if (tool === 'select' && selectedKind && isGuideColorEditable(selectedKind)) return 'selection'
  return null
}

export type ArrowPoint = { x: number; y: number }
export type ArrowDraft = {
  from: ArrowPoint
  to: ArrowPoint
  lo: number
  hi: number
}

export function arrowPress(point: ArrowPoint, at: number): ArrowDraft {
  return { from: point, to: point, lo: at, hi: at }
}

export function arrowMove(draft: ArrowDraft, point: ArrowPoint): ArrowDraft {
  return { ...draft, to: point }
}

export function arrowRelease(
  draft: ArrowDraft,
  point: ArrowPoint,
  at: number,
  minDistance: number,
): ArrowDraft | null {
  if (Math.hypot(point.x - draft.from.x, point.y - draft.from.y) < minDistance) {
    return null
  }
  return {
    ...draft,
    to: point,
    lo: Math.min(draft.lo, at),
    hi: Math.max(draft.hi, at),
  }
}
