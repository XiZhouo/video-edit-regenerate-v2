/**
 * Stable business intents for project namespace mutations.
 *
 * An intent outlives one fetch attempt: its opaque id, target id and
 * Idempotency-Key are created once, persisted in sessionStorage, and reused
 * until the server proves the operation committed/deleted or definitively
 * rejected it.  Keeping this logic DOM/React-free makes the safety rules easy
 * to regression-test.
 */

import { getT } from '@ze/i18n'

export type ProjectMutationKind = 'create' | 'duplicate' | 'delete' | 'demo-reset'
export type ProjectMutationIntentState =
  | 'prepared'
  | 'in_flight'
  | 'replay_in_flight'
  | 'unknown'
  | 'reconciling'
  | 'repair_required'

export type ProjectMutationIntent = {
  schemaVersion: 1
  intentId: string
  kind: ProjectMutationKind
  /** Local-only canonical action fingerprint; never sent as the idempotency key. */
  fingerprint: string
  /** Created/duplicated/deleted project id, including the catalog-bound demo sandbox id. */
  targetId: string
  sourceId?: string
  idempotencyKey: string
  state: ProjectMutationIntentState
  createdAt: number
  updatedAt: number
  lastErrorCode?: string
}

export type ProjectMutationIntentInput = {
  kind: ProjectMutationKind
  fingerprint: string
  targetId?: string
  sourceId?: string
}

export type ProjectMutationStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export type ProjectMutationWireStatus =
  | 'committed'
  | 'deleted'
  | 'in_progress'
  | 'repair_required'
  | 'not_committed'
  | 'conflict'

export type ProjectMutationReconciliation<T> =
  | { status: 'committed'; value: T }
  | { status: 'not_committed' }
  | { status: 'in_progress' }
  | { status: 'repair_required' | 'conflict'; message?: string }

const HEX_32 = /^[0-9a-f]{32}$/
const STATES = new Set<ProjectMutationIntentState>([
  'prepared', 'in_flight', 'replay_in_flight', 'unknown', 'reconciling', 'repair_required',
])
const KINDS = new Set<ProjectMutationKind>(['create', 'duplicate', 'delete', 'demo-reset'])

export class ProjectMutationStorageUnavailableError extends Error {
  readonly code = 'project_mutation_storage_unavailable'

  constructor(message = getT()('auth.projectMutation.storageUnavailable')) {
    super(message)
    this.name = 'ProjectMutationStorageUnavailableError'
  }
}

export class ProjectMutationPendingError extends Error {
  readonly code = 'project_mutation_pending'
  readonly intentId: string

  constructor(intent: ProjectMutationIntent) {
    super(getT()('auth.projectMutation.stillReconciling'))
    this.name = 'ProjectMutationPendingError'
    this.intentId = intent.intentId
  }
}

function defaultStorage(): ProjectMutationStorage | null {
  try {
    return globalThis.sessionStorage ?? null
  } catch {
    return null
  }
}

function requireStorage(
  storage: ProjectMutationStorage | null,
): ProjectMutationStorage {
  if (!storage) throw new ProjectMutationStorageUnavailableError()
  return storage
}

function randomHex32(): string {
  const cryptoApi = globalThis.crypto
  if (cryptoApi?.getRandomValues) {
    const bytes = new Uint8Array(16)
    cryptoApi.getRandomValues(bytes)
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  }
  const uuid = cryptoApi?.randomUUID?.().replaceAll('-', '').toLowerCase()
  if (uuid && HEX_32.test(uuid)) return uuid
  let value = ''
  while (value.length < 32) value += Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0')
  return value.slice(0, 32)
}

export function newProjectMutationIntentId(): string {
  return randomHex32()
}

export function projectMutationIdempotencyKey(
  kind: ProjectMutationKind,
  intentId: string,
): string {
  if (!HEX_32.test(intentId)) throw new Error('project mutation intent id must be 32 lowercase hex')
  return `web:project.${kind}:${intentId}`
}

/** New create/duplicate targets are opaque and permanently derived from the intent. */
export function projectTargetIdForIntent(intentId: string): string {
  if (!HEX_32.test(intentId)) throw new Error('project mutation intent id must be 32 lowercase hex')
  return `p_${intentId}`
}

/** Stable JSON for the small JSON-like inputs used by project actions. */
export function projectMutationFingerprint(
  kind: ProjectMutationKind,
  input: Record<string, unknown>,
): string {
  const canonical = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(canonical)
    if (value && typeof value === 'object') {
      return Object.fromEntries(
        Object.entries(value as Record<string, unknown>)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, nested]) => [key, canonical(nested)]),
      )
    }
    return value
  }
  return `${kind}:${JSON.stringify(canonical(input))}`
}

export function projectMutationStorageKey(ownerId: string): string {
  return `ze.project-mutation-intents.${ownerId || 'guest'}`
}

/** Same account on two tools/deployments must never share mutation evidence. */
export function projectMutationOwnerPartition(
  storageNamespace: string,
  ownerId: string,
): string {
  if (!storageNamespace.trim()) throw new Error('project storage namespace is required')
  if (!ownerId.trim()) throw new Error('project mutation owner is required')
  return `${encodeURIComponent(storageNamespace)}|${encodeURIComponent(ownerId)}`
}

function isIntent(value: unknown): value is ProjectMutationIntent {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<ProjectMutationIntent>
  return item.schemaVersion === 1
    && typeof item.intentId === 'string' && HEX_32.test(item.intentId)
    && typeof item.kind === 'string' && KINDS.has(item.kind as ProjectMutationKind)
    && typeof item.fingerprint === 'string' && item.fingerprint.length > 0
    && typeof item.targetId === 'string' && item.targetId.length > 0
    && typeof item.idempotencyKey === 'string'
    && item.idempotencyKey === projectMutationIdempotencyKey(item.kind as ProjectMutationKind, item.intentId)
    && typeof item.state === 'string' && STATES.has(item.state as ProjectMutationIntentState)
    && typeof item.createdAt === 'number' && Number.isFinite(item.createdAt)
    && typeof item.updatedAt === 'number' && Number.isFinite(item.updatedAt)
    && (item.sourceId === undefined || typeof item.sourceId === 'string')
    && (item.lastErrorCode === undefined || typeof item.lastErrorCode === 'string')
}

function readProjectMutationIntents(
  ownerId: string,
  storage: ProjectMutationStorage | null,
): ProjectMutationIntent[] {
  const durableStorage = requireStorage(storage)
  let raw: string | null
  try {
    raw = durableStorage.getItem(projectMutationStorageKey(ownerId))
  } catch {
    throw new ProjectMutationStorageUnavailableError(getT()('auth.projectMutation.storageUnreadable'))
  }
  if (raw === null) return []

  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    throw new ProjectMutationStorageUnavailableError(getT()('auth.projectMutation.storageCorrupt'))
  }
  if (!Array.isArray(value) || value.some((item) => !isIntent(item))) {
    throw new ProjectMutationStorageUnavailableError(getT()('auth.projectMutation.storageInvalid'))
  }
  return (value as ProjectMutationIntent[])
    .sort((left, right) => left.createdAt - right.createdAt)
}

export function inspectProjectMutationIntents(
  ownerId: string,
  storage: ProjectMutationStorage | null = defaultStorage(),
): { intents: ProjectMutationIntent[]; error: ProjectMutationStorageUnavailableError | null } {
  try {
    return { intents: readProjectMutationIntents(ownerId, storage), error: null }
  } catch (error) {
    return {
      intents: [],
      error: error instanceof ProjectMutationStorageUnavailableError
        ? error
        : new ProjectMutationStorageUnavailableError(),
    }
  }
}

export function loadProjectMutationIntents(
  ownerId: string,
  storage: ProjectMutationStorage | null = defaultStorage(),
): ProjectMutationIntent[] {
  if (!storage) return []
  try {
    const value: unknown = JSON.parse(storage.getItem(projectMutationStorageKey(ownerId)) ?? '[]')
    if (!Array.isArray(value)) return []
    return value.filter(isIntent).sort((left, right) => left.createdAt - right.createdAt)
  } catch {
    return []
  }
}

export function saveProjectMutationIntents(
  ownerId: string,
  intents: ProjectMutationIntent[],
  storage: ProjectMutationStorage | null = defaultStorage(),
): void {
  const durableStorage = requireStorage(storage)
  const key = projectMutationStorageKey(ownerId)
  try {
    if (intents.length === 0) {
      durableStorage.removeItem(key)
      return
    }
    if (intents.some((intent) => !isIntent(intent))) {
      throw new ProjectMutationStorageUnavailableError(
        getT()('auth.projectMutation.storageUnsafeOverwrite'),
      )
    }
    // Unresolved intents are safety tombstones.  Never prune an old one merely
    // to enforce a count cap: doing so would silently re-authorize a duplicate.
    const retained = [...intents]
      .sort((left, right) => left.updatedAt - right.updatedAt)
    durableStorage.setItem(key, JSON.stringify(retained))
  } catch (error) {
    if (error instanceof ProjectMutationStorageUnavailableError) throw error
    throw new ProjectMutationStorageUnavailableError(getT()('auth.projectMutation.storageUnwritable'))
  }
}

export function createProjectMutationIntent(
  input: ProjectMutationIntentInput,
  options: { intentId?: string; now?: number } = {},
): ProjectMutationIntent {
  const intentId = options.intentId ?? newProjectMutationIntentId()
  if (!HEX_32.test(intentId)) throw new Error('project mutation intent id must be 32 lowercase hex')
  if (!input.fingerprint) throw new Error('project mutation fingerprint is required')
  const targetId = input.targetId?.trim()
    || (input.kind === 'create' || input.kind === 'duplicate'
      ? projectTargetIdForIntent(intentId)
      : '')
  if (!targetId) throw new Error(`target id is required for ${input.kind}`)
  const now = options.now ?? Date.now()
  return {
    schemaVersion: 1,
    intentId,
    kind: input.kind,
    fingerprint: input.fingerprint,
    targetId,
    ...(input.sourceId ? { sourceId: input.sourceId } : {}),
    idempotencyKey: projectMutationIdempotencyKey(input.kind, intentId),
    state: 'prepared',
    createdAt: now,
    updatedAt: now,
  }
}

export function prepareProjectMutationIntent(
  ownerId: string,
  input: ProjectMutationIntentInput,
  storage: ProjectMutationStorage | null = defaultStorage(),
  options: { intentId?: string; now?: number } = {},
): { intent: ProjectMutationIntent; intents: ProjectMutationIntent[] } {
  const intents = readProjectMutationIntents(ownerId, storage)
  const existing = intents.find((item) => item.kind === input.kind
    && item.fingerprint === input.fingerprint
    && (!input.targetId || item.targetId === input.targetId)
    && (!input.sourceId || item.sourceId === input.sourceId))
  if (existing) return { intent: existing, intents }
  // One owner/tool partition has one topology transaction lane.  A different
  // fingerprint while any tombstone remains could hide/reorder the unresolved
  // outcome; only the exact original intent may continue.
  if (intents.length > 0) throw new ProjectMutationPendingError(intents[0])
  const intent = createProjectMutationIntent(input, options)
  const next = [...intents, intent]
  saveProjectMutationIntents(ownerId, next, storage)
  return { intent, intents: next }
}

export function transitionProjectMutationIntent(
  ownerId: string,
  intentId: string,
  state: ProjectMutationIntentState,
  lastErrorCode?: string,
  storage: ProjectMutationStorage | null = defaultStorage(),
  now = Date.now(),
): ProjectMutationIntent[] {
  let found = false
  const next = readProjectMutationIntents(ownerId, storage).map((intent) => {
    if (intent.intentId !== intentId) return intent
    found = true
    return {
      ...intent,
      state,
      updatedAt: now,
      ...(lastErrorCode ? { lastErrorCode } : { lastErrorCode: undefined }),
    }
  })
  if (!found) {
    throw new ProjectMutationStorageUnavailableError(
      getT()('auth.projectMutation.evidenceMissing'),
    )
  }
  saveProjectMutationIntents(ownerId, next, storage)
  return next
}

export function retireProjectMutationIntent(
  ownerId: string,
  intentId: string,
  storage: ProjectMutationStorage | null = defaultStorage(),
): ProjectMutationIntent[] {
  const next = readProjectMutationIntents(ownerId, storage)
    .filter((intent) => intent.intentId !== intentId)
  saveProjectMutationIntents(ownerId, next, storage)
  return next
}

export function projectMutationErrorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return 'unknown_error'
  const code = (error as { code?: unknown }).code
  return typeof code === 'string' && code ? code : 'unknown_error'
}

// HTTP status/code is never proof by itself: a proxy may replace a successful
// backend response with 401/408/409.  Releasing the tombstone requires the
// server's structured, intent-bound proof that no mutation committed.
export function isProjectMutationDefinitiveRejection(
  error: unknown,
  expectedIntentId?: string,
): boolean {
  if (!error || typeof error !== 'object') return false
  const context = (error as { context?: unknown }).context
  if (!context || typeof context !== 'object') return false
  const proof = context as { mutation_outcome?: unknown; intent_id?: unknown }
  return proof.mutation_outcome === 'not_started'
    && typeof proof.intent_id === 'string'
    && proof.intent_id.length > 0
    && (expectedIntentId === undefined || proof.intent_id === expectedIntentId)
}

export function isProjectMutationOutcomeUncertain(
  error: unknown,
  expectedIntentId?: string,
): boolean {
  return !isProjectMutationDefinitiveRejection(error, expectedIntentId)
}

export class ProjectMutationUnresolvedError extends Error {
  readonly code = 'project_mutation_unresolved'
  readonly intentId: string
  readonly outcome: 'in_progress' | 'repair_required' | 'unknown'

  constructor(
    intent: ProjectMutationIntent,
    outcome: 'in_progress' | 'repair_required' | 'unknown',
    message?: string,
  ) {
    super(message || getT()(
      outcome === 'repair_required'
        ? 'auth.projectMutation.repairRequired'
        : 'auth.projectMutation.stillReconciling',
    ))
    this.name = 'ProjectMutationUnresolvedError'
    this.intentId = intent.intentId
    this.outcome = outcome
  }
}

/**
 * Execute or reconcile one stable intent.  `not_committed` is the only status
 * that authorizes a new mutation attempt; all other unknown states are read-only.
 */
export async function runProjectMutationIntent<T>(options: {
  intent: ProjectMutationIntent
  execute: () => Promise<T>
  reconcile: () => Promise<ProjectMutationReconciliation<T>>
  transition: (state: ProjectMutationIntentState, errorCode?: string) => void
  retire: () => void
}): Promise<T> {
  const { intent, execute, reconcile, transition, retire } = options

  /**
   * A repair fence is monotonic.  Reconciliation may still discover a proven
   * commit and retire it, but no read outcome can silently restore write
   * authority.  In particular, do not persist a transient `reconciling` state:
   * a reload during the GET must retain the no-replay fence.
   */
  const reconcileFrozenOutcome = async (priorError?: unknown): Promise<T> => {
    let result: ProjectMutationReconciliation<T>
    try {
      result = await reconcile()
    } catch (error) {
      transition('repair_required', projectMutationErrorCode(priorError ?? error))
      throw priorError ?? error
    }
    if (result.status === 'committed') {
      retire()
      return result.value
    }

    const code = result.status === 'in_progress'
      ? 'idempotency_in_progress'
      : result.status === 'not_committed'
        ? 'project_mutation_retry_exhausted'
        : result.status
    transition('repair_required', code)
    const message = result.status === 'repair_required' || result.status === 'conflict'
      ? result.message
      : undefined
    throw new ProjectMutationUnresolvedError(intent, 'repair_required', message)
  }

  const reconcileOutcome = async (
    replayIfNotCommitted: boolean,
    priorError?: unknown,
  ): Promise<T> => {
    transition('reconciling', priorError ? projectMutationErrorCode(priorError) : undefined)
    let result: ProjectMutationReconciliation<T>
    try {
      result = await reconcile()
    } catch (error) {
      if (projectMutationErrorCode(error) === 'guest_session_required'
          && replayIfNotCommitted) {
        // The server proves no demo mutation ran without a stable guest
        // identity.  POST once with the same identity; it may establish the
        // cookie and ask for one final same-intent retry.
        return attempt(true, false)
      }
      transition('unknown', projectMutationErrorCode(priorError ?? error))
      throw priorError ?? error
    }
    if (result.status === 'committed') {
      retire()
      return result.value
    }
    if (result.status === 'repair_required' || result.status === 'conflict') {
      transition('repair_required', result.status)
      throw new ProjectMutationUnresolvedError(intent, 'repair_required', result.message)
    }
    if (result.status === 'in_progress') {
      transition('unknown', 'idempotency_in_progress')
      throw new ProjectMutationUnresolvedError(intent, 'in_progress')
    }
    if (!replayIfNotCommitted) {
      transition('repair_required', 'project_mutation_retry_exhausted')
      throw new ProjectMutationUnresolvedError(intent, 'repair_required')
    }
    return attempt(false, false)
  }

  const attempt = async (
    allowGuestSessionRetry: boolean,
    replayIfNotCommitted: boolean,
  ): Promise<T> => {
    // `replay_in_flight` durably records that the one server-authorized replay
    // has already been consumed.  A reload in the middle can reconcile it but
    // can never grant a second replay.
    transition(replayIfNotCommitted ? 'in_flight' : 'replay_in_flight')
    try {
      const value = await execute()
      retire()
      return value
    } catch (error) {
      const code = projectMutationErrorCode(error)
      if (code === 'guest_session_required' && allowGuestSessionRetry) {
        // The 409 response only establishes the HttpOnly guest cookie and
        // explicitly proves that no namespace mutation ran.
        transition('reconciling', code)
        return attempt(false, replayIfNotCommitted)
      }
      if (!isProjectMutationOutcomeUncertain(error, intent.intentId)) {
        retire()
        throw error
      }
      if (!replayIfNotCommitted) {
        transition('repair_required', code)
        return reconcileFrozenOutcome(error)
      }
      transition('unknown', code)
      return reconcileOutcome(replayIfNotCommitted, error)
    }
  }

  // A reload may leave in_flight behind.  Never assume that means the request
  // did not arrive: all persisted non-prepared states reconcile first.
  if (intent.state === 'repair_required' || intent.state === 'replay_in_flight') {
    return reconcileFrozenOutcome()
  }
  if (intent.state !== 'prepared') return reconcileOutcome(true)
  return attempt(true, true)
}
