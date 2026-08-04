/**
 * Shared view-state store — versioned per-user × per-skill × per-project × per-slot preferences.
 *
 * View state is personal UI state, never Document/Operation data. One collision-safe localStorage
 * envelope owns every editor slice for a slot; each slice carries its own version and migration.
 * The envelope repeats the full scope and is rejected unless it exactly matches the requested
 * scope, so a malformed/tampered key cannot leak another user or project's state.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

export const VIEW_STATE_SCHEMA = 'ze.view-state.scope'
export const VIEW_STATE_ENVELOPE_VERSION = 1
const VIEW_STATE_PREFIX = 'ze.view-state.scope:'
const STORAGE_IDENTITIES = new WeakMap<object, number>()
let nextStorageIdentity = 1

export type ViewStateScope = Readonly<{
  userId: string
  skillId: string
  projectId: string
  slotId: string
}>

export type ViewStateStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export type ViewStateSliceCodec<T> = Readonly<{
  version: number
  /** Return null when persisted bytes are not a valid current value. */
  decode: (value: unknown) => T | null
  /** Optional ordered migration into the current value. Unknown versions must return null. */
  migrate?: (value: unknown, fromVersion: number) => T | null
}>

export type ViewStateReadResult<T> = {
  value: T
  found: boolean
  migrated: boolean
}

type StoredSlice = { version: number; value: unknown }
type ViewStateEnvelope = {
  schema: typeof VIEW_STATE_SCHEMA
  version: typeof VIEW_STATE_ENVELOPE_VERSION
  scope: ViewStateScope
  slices: Record<string, StoredSlice>
}

type ParsedEnvelope = { envelope: ViewStateEnvelope; migratedEnvelope: boolean }

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

export function isViewStateScope(scope: ViewStateScope | undefined): scope is ViewStateScope {
  return !!scope
    && [scope.userId, scope.skillId, scope.projectId, scope.slotId]
      .every((part) => typeof part === 'string' && part.length > 0)
}

/** URI encoding is injective for each segment; ':' is always encoded inside a segment. */
export function viewStateStorageKey(scope: ViewStateScope): string | null {
  if (!isViewStateScope(scope)) return null
  return VIEW_STATE_PREFIX + [scope.userId, scope.skillId, scope.projectId, scope.slotId]
    .map((part) => encodeURIComponent(part))
    .join(':')
}

function sameScope(a: unknown, b: ViewStateScope): boolean {
  const rec = asRecord(a)
  return !!rec
    && rec.userId === b.userId
    && rec.skillId === b.skillId
    && rec.projectId === b.projectId
    && rec.slotId === b.slotId
}

function validSliceRecord(value: unknown): StoredSlice | null {
  const rec = asRecord(value)
  if (!rec || !Number.isInteger(rec.version) || (rec.version as number) < 0 || !('value' in rec)) return null
  return { version: rec.version as number, value: rec.value }
}

function parseEnvelope(raw: string | null, scope: ViewStateScope): ParsedEnvelope | null {
  if (!raw) return null
  let parsed: unknown
  try { parsed = JSON.parse(raw) } catch { return null }
  const rec = asRecord(parsed)
  if (!rec || rec.schema !== VIEW_STATE_SCHEMA || !sameScope(rec.scope, scope)) return null

  if (rec.version === VIEW_STATE_ENVELOPE_VERSION) {
    const rawSlices = asRecord(rec.slices)
    if (!rawSlices) return null
    const slices: Record<string, StoredSlice> = {}
    for (const [name, value] of Object.entries(rawSlices)) {
      const slice = validSliceRecord(value)
      if (slice) slices[name] = slice
    }
    return { envelope: { schema: VIEW_STATE_SCHEMA, version: 1, scope, slices }, migratedEnvelope: false }
  }

  // Envelope v0 used `values: {slice: rawValue}` without per-slice versions. Convert every value
  // to slice version 0; the owning editor's codec then decides whether that value is migratable.
  if (rec.version === 0) {
    const values = asRecord(rec.values)
    if (!values) return null
    const slices = Object.fromEntries(
      Object.entries(values).map(([name, value]) => [name, { version: 0, value }]),
    )
    return { envelope: { schema: VIEW_STATE_SCHEMA, version: 1, scope, slices }, migratedEnvelope: true }
  }
  return null
}

function browserStorage(): ViewStateStorage | undefined {
  try { return globalThis.localStorage } catch { return undefined }
}

function storageIdentity(storage: ViewStateStorage | undefined): string {
  if (!storage) return 'none'
  let identity = STORAGE_IDENTITIES.get(storage)
  if (identity === undefined) {
    identity = nextStorageIdentity++
    STORAGE_IDENTITIES.set(storage, identity)
  }
  return String(identity)
}

function readEnvelope(storage: ViewStateStorage, scope: ViewStateScope): ParsedEnvelope | null {
  const key = viewStateStorageKey(scope)
  if (!key) return null
  try { return parseEnvelope(storage.getItem(key), scope) } catch { return null }
}

function persistEnvelope(storage: ViewStateStorage, envelope: ViewStateEnvelope): boolean {
  const key = viewStateStorageKey(envelope.scope)
  if (!key) return false
  try {
    storage.setItem(key, JSON.stringify(envelope))
    return true
  } catch {
    return false
  }
}

export function readViewStateSlice<T>(
  scope: ViewStateScope,
  sliceName: string,
  codec: ViewStateSliceCodec<T>,
  fallback: T,
  storage: ViewStateStorage | undefined = browserStorage(),
): ViewStateReadResult<T> {
  if (!storage || !sliceName || !isViewStateScope(scope)) return { value: fallback, found: false, migrated: false }
  const parsed = readEnvelope(storage, scope)
  const stored = parsed?.envelope.slices[sliceName]
  if (!parsed || !stored) return { value: fallback, found: false, migrated: false }

  const migratedSlice = stored.version !== codec.version
  const decoded = migratedSlice
    ? codec.migrate?.(stored.value, stored.version) ?? null
    : codec.decode(stored.value)
  if (decoded === null) return { value: fallback, found: false, migrated: false }

  const migrated = parsed.migratedEnvelope || migratedSlice
  if (migrated) {
    parsed.envelope.slices[sliceName] = { version: codec.version, value: decoded }
    persistEnvelope(storage, parsed.envelope)
  }
  return { value: decoded, found: true, migrated }
}

export function writeViewStateSlice<T>(
  scope: ViewStateScope,
  sliceName: string,
  codec: ViewStateSliceCodec<T>,
  value: T,
  storage: ViewStateStorage | undefined = browserStorage(),
): boolean {
  if (!storage || !sliceName || !isViewStateScope(scope)) return false
  const decoded = codec.decode(value)
  if (decoded === null) return false
  const envelope = readEnvelope(storage, scope)?.envelope ?? {
    schema: VIEW_STATE_SCHEMA,
    version: VIEW_STATE_ENVELOPE_VERSION,
    scope,
    slices: {},
  }
  envelope.slices[sliceName] = { version: codec.version, value: decoded }
  return persistEnvelope(storage, envelope)
}

/** Remove one personal slice without disturbing sibling editor preferences in the same slot. */
export function removeViewStateSlice(
  scope: ViewStateScope,
  sliceName: string,
  storage: ViewStateStorage | undefined = browserStorage(),
): boolean {
  if (!storage || !sliceName || !isViewStateScope(scope)) return false
  const parsed = readEnvelope(storage, scope)
  if (!parsed || !(sliceName in parsed.envelope.slices)) return false
  delete parsed.envelope.slices[sliceName]
  const key = viewStateStorageKey(scope)
  if (!key) return false
  try {
    if (Object.keys(parsed.envelope.slices).length === 0) storage.removeItem(key)
    else storage.setItem(key, JSON.stringify(parsed.envelope))
    return true
  } catch {
    return false
  }
}

export type ScopedViewStateSetter<T> = (
  next: T | ((previous: T) => T),
  options?: { persist?: boolean },
) => void

export type UseScopedViewStateOptions<T> = {
  scope?: ViewStateScope
  slice: string
  codec: ViewStateSliceCodec<T>
  defaultValue: T
  storage?: ViewStateStorage
}

export type ScopedViewState<T> = {
  value: T
  setValue: ScopedViewStateSetter<T>
  /** Delete the durable slice and restore this hook's default value for the current identity. */
  resetValue: () => void
  /** True only when the current scope has a valid durable slice. Transient updates do not set it. */
  hasStored: boolean
  /** Stable identity for resetting scope-specific initialization such as first-open auto-fit. */
  identity: string
}

type HookEntry<T> = { identity: string; value: T; hasStored: boolean }

/**
 * React bridge with an important switch invariant: when scope changes, the value returned during
 * that very render is loaded for the new identity. Old scope state is never rendered or used as
 * the base of a write while an effect catches up.
 */
export function useScopedViewState<T>(options: UseScopedViewStateOptions<T>): ScopedViewState<T> {
  const storage = options.storage ?? browserStorage()
  const scopeKey = options.scope ? viewStateStorageKey(options.scope) : null
  // Storage is part of the authority. Swapping an injected storage object with the same logical
  // scope must synchronously load from the new authority rather than keep the old object's value.
  const identity = `${storageIdentity(storage)}\u0000${scopeKey ?? 'memory'}\u0000${options.slice}\u0000${options.codec.version}`
  const latest = useRef(options)
  latest.current = options

  const loaded = useMemo<HookEntry<T>>(() => {
    const current = latest.current
    if (!scopeKey || !current.scope) {
      return { identity, value: current.defaultValue, hasStored: false }
    }
    const read = readViewStateSlice(current.scope, current.slice, current.codec, current.defaultValue, storage)
    return { identity, value: read.value, hasStored: read.found }
    // identity contains scope/slice/version; storage is an injectable authority in tests/embedders.
  }, [identity, scopeKey, storage])

  const [entry, setEntry] = useState<HookEntry<T>>(() => loaded)
  const current = entry.identity === identity ? entry : loaded
  const currentRef = useRef(current)
  currentRef.current = current
  const liveIdentityRef = useRef(identity)
  liveIdentityRef.current = identity

  useEffect(() => {
    // A child layout effect can write the new identity before this passive synchronization runs.
    // Preserve that newer entry; only replace an entry that still belongs to the previous scope.
    setEntry((currentEntry) => currentEntry.identity === identity ? currentEntry : loaded)
  }, [identity, loaded])

  const setValue = useCallback<ScopedViewStateSetter<T>>((next, behavior) => {
    // Debounced/async consumers may retain an old setter across a project switch. Such a setter
    // belongs to the old authority and must become inert instead of writing its bytes to the new
    // scope held in `latest.current`.
    if (liveIdentityRef.current !== identity) return
    const config = latest.current
    const base = currentRef.current.identity === identity ? currentRef.current : loaded
    const value = typeof next === 'function'
      ? (next as (previous: T) => T)(base.value)
      : next
    const wantsPersistence = behavior?.persist !== false
    const persisted = wantsPersistence && !!scopeKey && !!config.scope
      ? writeViewStateSlice(config.scope, config.slice, config.codec, value, storage)
      : false
    const nextEntry = {
      identity,
      value,
      hasStored: base.hasStored || persisted,
    }
    // React may batch multiple disclosure updates from one user action. Advance the synchronous
    // base before scheduling the render so a second functional setter composes with the first
    // instead of overwriting it with the pre-event snapshot.
    currentRef.current = nextEntry
    setEntry(nextEntry)
  }, [identity, loaded, scopeKey, storage])

  const resetValue = useCallback(() => {
    if (liveIdentityRef.current !== identity) return
    const config = latest.current
    if (scopeKey && config.scope) removeViewStateSlice(config.scope, config.slice, storage)
    const nextEntry = { identity, value: config.defaultValue, hasStored: false }
    currentRef.current = nextEntry
    setEntry(nextEntry)
  }, [identity, scopeKey, storage])

  return { value: current.value, setValue, resetValue, hasStored: current.hasStored, identity }
}
