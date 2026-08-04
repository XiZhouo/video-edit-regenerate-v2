import type { ServerStatus } from '@ze/core/04-user-facing/01-layout/02-footer'
import type { SyncState } from '@ze/core/04-user-facing/01-layout/syncState'
import { SYNC_DELAY_MS } from '@ze/core/04-user-facing/01-layout/syncState'

export type EventStreamStatus = 'idle' | 'connecting' | 'open' | 'error'

export type PendingSyncRecord = {
  idempotencyKey: string
  commandId: string
  startedAt: number
  httpAck: boolean
  streamAck: boolean
}

export type SyncConflictRecord = {
  traceId: string
  at: number
}

export type SyncStateInput = {
  projectId: string | null
  serverStatus: ServerStatus
  streamStatus: EventStreamStatus
  pending: PendingSyncRecord[]
  conflicts: SyncConflictRecord[]
  now: number
  delayMs?: number
}

export function deriveSyncState(input: SyncStateInput): SyncState {
  if (!input.projectId) return { kind: 'unavailable' }
  if (input.serverStatus === 'offline' || input.streamStatus === 'error') return { kind: 'offline' }
  if (input.conflicts.length > 0) {
    return {
      kind: 'conflict',
      conflict_count: input.conflicts.length,
      trace_ids: input.conflicts.map((c) => c.traceId).filter(Boolean),
    }
  }

  const pending = input.pending
  if (pending.length > 0) {
    const delayMs = input.delayMs ?? SYNC_DELAY_MS
    const oldest = pending.reduce((min, p) => Math.min(min, p.startedAt), pending[0].startedAt)
    const age = Math.max(0, input.now - oldest)
    if (age >= delayMs) {
      return { kind: 'delayed', delay_ms: age, pending_op_count: pending.length }
    }
    return { kind: 'syncing', pending_op_count: pending.length }
  }

  if (input.streamStatus === 'open') return { kind: 'in-sync' }
  return { kind: 'unavailable' }
}
