import { describe, expect, it } from 'vitest'
import {
  GENERATION_MODELS,
  hasStrategyChoice,
  selectionForModel,
  strategiesForModel,
} from './generationOptions'

describe('generation model options', () => {
  it('shows exactly Omni, Seedance Pro and Seedance Fast', () => {
    expect(GENERATION_MODELS.map((option) => option.id)).toEqual([
      'gemini-omni-flash-preview',
      'seedance-2.0',
      'seedance-2.0-fast',
    ])
  })

  it('maps the selected model to its internal provider', () => {
    expect(selectionForModel('gemini-omni-flash-preview', 'annotated_video'))
      .toMatchObject({ provider: 'omni', strategy: 'annotated_video', mode: 1 })
    expect(selectionForModel('seedance-2.0', 'annotated_video'))
      .toMatchObject({ provider: 'seedance', strategy: 'annotated_video', mode: 1 })
    expect(selectionForModel('seedance-2.0-fast', 'modified_frames'))
      .toMatchObject({ provider: 'seedance', strategy: 'modified_frames', mode: 2 })
  })

  it('makes both submission modes available for every model', () => {
    expect(strategiesForModel('seedance-2.0'))
      .toEqual(['annotated_video', 'modified_frames'])
    expect(strategiesForModel('gemini-omni-flash-preview'))
      .toEqual(['annotated_video', 'modified_frames'])
    expect(selectionForModel('gemini-omni-flash-preview', 'modified_frames'))
      .toMatchObject({ provider: 'omni', strategy: 'modified_frames', mode: 2 })
  })

  it('shows the submission-mode chooser for every model', () => {
    expect(hasStrategyChoice('gemini-omni-flash-preview')).toBe(true)
    expect(hasStrategyChoice('seedance-2.0')).toBe(true)
    expect(hasStrategyChoice('seedance-2.0-fast')).toBe(true)
  })
})
