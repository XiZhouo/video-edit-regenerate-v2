import { describe, expect, it, vi } from 'vitest'
import { submitPromptOptimizationRequest } from './promptOptimizationRequest'

describe('prompt optimization request submission', () => {
  it('rejects a command failure swallowed by the shared dispatch wrapper', async () => {
    const dispatch = vi.fn(async () => undefined)

    await expect(submitPromptOptimizationRequest(dispatch, { requestId: 'opt-1' }))
      .resolves.toBe(false)
    expect(dispatch).toHaveBeenCalledWith('prompt.optimize', { requestId: 'opt-1' })
  })

  it('accepts a command only when the backend returns a command result', async () => {
    const dispatch = vi.fn(async () => ({ status: 'applied' }))

    await expect(submitPromptOptimizationRequest(dispatch, { requestId: 'opt-1' }))
      .resolves.toBe(true)
  })
})
