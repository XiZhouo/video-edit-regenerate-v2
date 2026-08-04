import type { CredentialProvider } from '@ze/core/04-user-facing/03-authentication'

export type OptimizerProvider = Extract<
  CredentialProvider,
  'codex_cli_profile' | 'claude_cli_profile' | 'cursor_api_key'
>

export const OPTIMIZER_PROVIDERS: OptimizerProvider[] = [
  'codex_cli_profile',
  'claude_cli_profile',
  'cursor_api_key',
]

type StorageReader = Pick<Storage, 'getItem'>
type StorageWriter = Pick<Storage, 'getItem' | 'setItem'>

export type OptimizerPrefs = {
  version: 2
  provider?: OptimizerProvider
  models: Partial<Record<OptimizerProvider, string>>
}

const OPTIMIZER_PREF_KEY = 'ze.ves.optimizer.prefs'

function preferenceKey(userId: string): string {
  return `${OPTIMIZER_PREF_KEY}:${encodeURIComponent(userId)}`
}

function emptyPrefs(): OptimizerPrefs {
  return { version: 2, models: {} }
}

export function readOptimizerPrefs(
  storage: StorageReader | undefined,
  userId: string,
): OptimizerPrefs {
  if (!storage || !userId) return emptyPrefs()
  try {
    const raw = storage.getItem(preferenceKey(userId))
    if (!raw) return emptyPrefs()
    const parsed = JSON.parse(raw) as Partial<OptimizerPrefs>
    const provider = OPTIMIZER_PROVIDERS.includes(parsed.provider as OptimizerProvider)
      ? parsed.provider as OptimizerProvider
      : undefined
    const models = Object.fromEntries(
      OPTIMIZER_PROVIDERS.flatMap((candidate) => {
        const value = parsed.models?.[candidate]
        return typeof value === 'string' ? [[candidate, value]] : []
      }),
    ) as OptimizerPrefs['models']
    return { version: 2, provider, models }
  } catch {
    return emptyPrefs()
  }
}

export function modelForOptimizer(
  prefs: OptimizerPrefs,
  provider: OptimizerProvider,
): string {
  return prefs.models[provider] || ''
}

export function saveOptimizerSelection(
  storage: StorageWriter | undefined,
  userId: string,
  provider: OptimizerProvider,
  model: string,
): void {
  if (!storage || !userId) return
  try {
    const current = readOptimizerPrefs(storage, userId)
    storage.setItem(preferenceKey(userId), JSON.stringify({
      version: 2,
      provider,
      models: { ...current.models, [provider]: model },
    } satisfies OptimizerPrefs))
  } catch {
    /* private mode / disabled storage — keep in-memory only */
  }
}
