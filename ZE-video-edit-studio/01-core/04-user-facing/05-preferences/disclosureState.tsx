/**
 * Shared disclosure state — durable open/closed preferences for editor sections.
 *
 * Identity is always an explicit three-part declaration tuple:
 *   ownerId (framework surface) × contextId (for example entity type) × stateKey (section id).
 * The tuple is JSON encoded, so delimiters inside any part cannot collide. Titles and render
 * order are deliberately absent: translated copy may change and array ordinals are not identity.
 *
 * The resulting slice still lives inside the shared four-axis view-state envelope
 * (user × skill × project × slot). It is personal UI state, never Document/Operation state.
 */

import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'
import {
  useScopedViewState,
  type ViewStateScope,
  type ViewStateSliceCodec,
  type ViewStateStorage,
} from './viewStateStore'

export const DISCLOSURE_VIEW_STATE_SLICE = 'editor.disclosures'

export type DisclosureViewState = Readonly<Record<string, boolean>>

function nonEmptyId(value: string): boolean {
  return value.trim().length > 0
}

/** Injective key for an exact declaration tuple; null means the declaration has no stable id. */
export function disclosureStateKey(ownerId: string, contextId: string, stateKey: string): string | null {
  if (![ownerId, contextId, stateKey].every(nonEmptyId)) return null
  return JSON.stringify([ownerId, contextId, stateKey])
}

function decodeDisclosureKey(key: string): [string, string, string] | null {
  let parsed: unknown
  try { parsed = JSON.parse(key) } catch { return null }
  if (!Array.isArray(parsed) || parsed.length !== 3 || !parsed.every((part) => typeof part === 'string' && nonEmptyId(part))) {
    return null
  }
  const tuple = parsed as [string, string, string]
  return disclosureStateKey(...tuple) === key ? tuple : null
}

export const disclosureViewStateCodec: ViewStateSliceCodec<DisclosureViewState> = {
  version: 1,
  decode: (value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const out: Record<string, boolean> = {}
    for (const [key, open] of Object.entries(value as Record<string, unknown>)) {
      if (!decodeDisclosureKey(key) || typeof open !== 'boolean') return null
      out[key] = open
    }
    return out
  },
}

export type UseScopedDisclosureGroupOptions = Readonly<{
  scope?: ViewStateScope
  /** Stable framework-owned surface id, for example `inspector.sections`. */
  ownerId: string
  /** Stable declaration context, for example an entity type; never a visible title. */
  contextId: string
  storage?: ViewStateStorage
}>

export type ScopedDisclosureGroup = Readonly<{
  openFor: (stateKey: string, defaultOpen?: boolean) => boolean
  setOpen: (stateKey: string, open: boolean) => void
  toggle: (stateKey: string, defaultOpen?: boolean) => void
  hasStored: boolean
  identity: string
}>

function hasOwn(record: DisclosureViewState, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key)
}

/**
 * Collection-level hook for editors such as Outline that need to query many stable item ids from
 * one hook call. Components rendering a single disclosure normally use DisclosureStateProvider.
 */
export function useScopedDisclosureGroup(options: UseScopedDisclosureGroupOptions): ScopedDisclosureGroup {
  const { scope, ownerId, contextId, storage } = options
  const state = useScopedViewState<DisclosureViewState>({
    scope,
    slice: DISCLOSURE_VIEW_STATE_SLICE,
    codec: disclosureViewStateCodec,
    defaultValue: {},
    storage,
  })

  const keyFor = useCallback((stateKey: string) => (
    disclosureStateKey(ownerId, contextId, stateKey)
  ), [ownerId, contextId])

  const openFor = useCallback((stateKey: string, defaultOpen = true) => {
    const key = keyFor(stateKey)
    return key && hasOwn(state.value, key) ? state.value[key] : defaultOpen
  }, [keyFor, state.value])

  const setOpen = useCallback((stateKey: string, open: boolean) => {
    const key = keyFor(stateKey)
    if (!key) return
    state.setValue((previous) => ({ ...previous, [key]: open }))
  }, [keyFor, state.setValue])

  const toggle = useCallback((stateKey: string, defaultOpen = true) => {
    const key = keyFor(stateKey)
    if (!key) return
    state.setValue((previous) => ({
      ...previous,
      [key]: !(hasOwn(previous, key) ? previous[key] : defaultOpen),
    }))
  }, [keyFor, state.setValue])

  return useMemo(() => ({
    openFor,
    setOpen,
    toggle,
    hasStored: state.hasStored,
    identity: `${state.identity}\u0000${JSON.stringify([ownerId, contextId])}`,
  }), [contextId, openFor, ownerId, setOpen, state.hasStored, state.identity, toggle])
}

const DisclosureContext = createContext<ScopedDisclosureGroup | null>(null)

export type DisclosureStateProviderProps = UseScopedDisclosureGroupOptions & Readonly<{
  children: ReactNode
}>

/** Supplies one collision-free disclosure group to descendant Collapsible primitives. */
export function DisclosureStateProvider({
  scope, ownerId, contextId, storage, children,
}: DisclosureStateProviderProps): ReactNode {
  const group = useScopedDisclosureGroup({ scope, ownerId, contextId, storage })
  return <DisclosureContext.Provider value={group}>{children}</DisclosureContext.Provider>
}

export type DisclosureItemState = Readonly<{
  open: boolean
  setOpen: (open: boolean) => void
  toggle: () => void
}>

/**
 * Reads a single explicitly identified item from the nearest provider. Missing provider/key means
 * "not persistent"; callers must keep their normal uncontrolled fallback and must not infer an id.
 */
export function useDisclosureItemState(
  stateKey: string | undefined,
  defaultOpen = true,
): DisclosureItemState | null {
  const group = useContext(DisclosureContext)
  const validStateKey = typeof stateKey === 'string' && nonEmptyId(stateKey) ? stateKey : null
  const open = group && validStateKey ? group.openFor(validStateKey, defaultOpen) : defaultOpen
  const setOpen = useCallback((next: boolean) => {
    if (group && validStateKey) group.setOpen(validStateKey, next)
  }, [group, validStateKey])
  const toggle = useCallback(() => {
    if (group && validStateKey) group.toggle(validStateKey, defaultOpen)
  }, [defaultOpen, group, validStateKey])
  return group && validStateKey ? { open, setOpen, toggle } : null
}
