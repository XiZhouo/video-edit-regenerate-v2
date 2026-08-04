import { describe, expect, it } from 'vitest'
import {
  modelForOptimizer,
  readOptimizerPrefs,
  saveOptimizerSelection,
} from './optimizerPrefs'

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }
}

describe('optimizer preferences', () => {
  it('keeps selections isolated between users', () => {
    const storage = new MemoryStorage()

    saveOptimizerSelection(storage, 'user-a', 'codex_cli_profile', 'gpt-5.4')
    saveOptimizerSelection(storage, 'user-b', 'claude_cli_profile', 'sonnet')

    expect(readOptimizerPrefs(storage, 'user-a')).toEqual({
      version: 2,
      provider: 'codex_cli_profile',
      models: { codex_cli_profile: 'gpt-5.4' },
    })
    expect(readOptimizerPrefs(storage, 'user-b')).toEqual({
      version: 2,
      provider: 'claude_cli_profile',
      models: { claude_cli_profile: 'sonnet' },
    })
  })

  it('keeps a separate model for every provider', () => {
    const storage = new MemoryStorage()

    saveOptimizerSelection(storage, 'user-a', 'codex_cli_profile', 'gpt-5.4')
    saveOptimizerSelection(storage, 'user-a', 'cursor_api_key', 'cursor-small')

    const prefs = readOptimizerPrefs(storage, 'user-a')
    expect(modelForOptimizer(prefs, 'codex_cli_profile')).toBe('gpt-5.4')
    expect(modelForOptimizer(prefs, 'cursor_api_key')).toBe('cursor-small')
  })

  it('does not inherit the old global preference into a signed-in user', () => {
    const storage = new MemoryStorage()
    storage.setItem('ze.ves.optimizer.prefs', JSON.stringify({
      provider: 'cursor_api_key',
      model: 'private-model',
    }))

    expect(readOptimizerPrefs(storage, 'user-a')).toEqual({
      version: 2,
      models: {},
    })
  })
})
