import { describe, expect, it } from 'vitest'
import { toolActivationPatch } from './studioStore'

describe('canvas tool activation', () => {
  it('leaves selection intact for select mode and clears it for creation tools', () => {
    expect(toolActivationPatch('select')).toEqual({ tool: 'select' })
    expect(toolActivationPatch('text')).toEqual({ tool: 'text', picked: null })
    expect(toolActivationPatch('rect')).toEqual({ tool: 'rect', picked: null })
    expect(toolActivationPatch('arrow')).toEqual({ tool: 'arrow', picked: null })
  })
})
