/**
 * PersistenceState — 当前文档“是否已经可靠保存”的前端投影。
 *
 * 它只消费文档元数据和 mutation HTTP 回执，刻意不读取 SyncState：
 * “协作事件是否追上”与“这次修改是否已经持久化”是两件不同的事。
 */

export type SavePolicy = 'auto_commit' | 'checkpoint_commit' | 'legacy'
export type CommitStatus = 'committed' | 'dirty' | 'aborted' | 'indeterminate'
export type EventStatus = 'not_required' | 'pending' | 'published' | 'failed'

export type PersistenceReceipt = {
  /** `clean` is the additive W02 compatibility value; W03 returns CommitStatus. */
  status: CommitStatus | 'clean'
  document_version: number | null
  save_policy: SavePolicy
  document_durable: boolean
  oplog_durable?: boolean
  retry_safe: boolean
  event_status?: EventStatus
  commit_id?: string
  operation_ids?: string[]
  failure?: { code?: string; message?: string } | null
}

export type PersistenceFacts = {
  document_version: number | null
  save_policy: SavePolicy
  document_durable: boolean
  retry_safe: boolean
}

export type PersistenceState =
  | { kind: 'unavailable' }
  | ({ kind: 'clean' } & PersistenceFacts)
  | ({ kind: 'dirty' } & PersistenceFacts)
  | ({ kind: 'repair-required'; reason: 'indeterminate' | 'missing-receipt' } & Partial<PersistenceFacts>)
  | {
      kind: 'committing'
      pending_count: number
      previous_clean?: Extract<PersistenceState, { kind: 'clean' }>
    }
  | {
      kind: 'failed'
      http_status: number
      error_code: string
      previous_clean?: Extract<PersistenceState, { kind: 'clean' }>
    }

export type PersistenceDocumentMeta = {
  version: number
  save_policy: SavePolicy | null
  effective_save_policy: SavePolicy
  auto_commit_enabled: boolean
}

type SettledPersistenceState = Exclude<PersistenceState, { kind: 'committing' }>

/**
 * active_request_ids 留在 reducer runtime 中，不塞进 UI 状态语义。
 * 这样两个不同 mutation 同时在飞时，一个先返回也不会让 UI 过早显示“已保存”。
 */
export type PersistenceRuntime = {
  state: PersistenceState
  settled: SettledPersistenceState
  active_request_ids: string[]
}

export type PersistenceEvent =
  | { type: 'reset'; meta: PersistenceDocumentMeta | null }
  | { type: 'begin'; request_id: string }
  | { type: 'pending-confirmation'; request_id: string }
  | { type: 'success'; request_id: string; receipt?: PersistenceReceipt }
  | { type: 'failure'; request_id: string; http_status: number; error_code: string }

function facts(receipt: PersistenceReceipt): PersistenceFacts {
  return {
    document_version: receipt.document_version,
    save_policy: receipt.save_policy,
    document_durable: receipt.document_durable,
    retry_safe: receipt.retry_safe,
  }
}

export function persistenceFromReceipt(receipt?: PersistenceReceipt): SettledPersistenceState {
  if (!receipt) return { kind: 'repair-required', reason: 'missing-receipt' }
  if (receipt.status === 'indeterminate') {
    return { kind: 'repair-required', reason: 'indeterminate', ...facts(receipt) }
  }
  if (receipt.status === 'aborted') {
    return {
      kind: 'failed',
      http_status: 0,
      error_code: receipt.failure?.code || 'commit_aborted',
    }
  }
  return {
    kind: receipt.status === 'committed' ? 'clean' : receipt.status,
    ...facts(receipt),
  }
}

export function persistenceFromMeta(meta: PersistenceDocumentMeta | null): SettledPersistenceState {
  if (!meta) return { kind: 'unavailable' }
  return {
    kind: 'clean',
    document_version: meta.version,
    save_policy: meta.effective_save_policy,
    document_durable: true,
    retry_safe: true,
  }
}

export function createPersistenceRuntime(meta: PersistenceDocumentMeta | null): PersistenceRuntime {
  const settled = persistenceFromMeta(meta)
  return { state: settled, settled, active_request_ids: [] }
}

function previousClean(settled: SettledPersistenceState): Extract<PersistenceState, { kind: 'clean' }> | undefined {
  if (settled.kind === 'clean') return settled
  if (settled.kind === 'failed') return settled.previous_clean
  return undefined
}

function committing(settled: SettledPersistenceState, active: string[]): PersistenceState {
  return {
    kind: 'committing',
    pending_count: active.length,
    previous_clean: previousClean(settled),
  }
}

function without(active: string[], requestId: string): string[] {
  return active.filter((id) => id !== requestId)
}

/** 纯 reducer：网络 hook 只负责把已发生的事实翻成 event。 */
export function reducePersistence(runtime: PersistenceRuntime, event: PersistenceEvent): PersistenceRuntime {
  if (event.type === 'reset') return createPersistenceRuntime(event.meta)

  if (event.type === 'begin') {
    if (runtime.active_request_ids.includes(event.request_id)) return runtime
    const active = [...runtime.active_request_ids, event.request_id]
    return { ...runtime, active_request_ids: active, state: committing(runtime.settled, active) }
  }

  // 忽略已被 reset 或已经结算的旧响应，避免跨项目/重复响应篡改当前保存状态。
  if (!runtime.active_request_ids.includes(event.request_id)) return runtime
  const active = without(runtime.active_request_ids, event.request_id)

  if (event.type === 'pending-confirmation') {
    return {
      ...runtime,
      active_request_ids: active,
      state: active.length > 0 ? committing(runtime.settled, active) : runtime.settled,
    }
  }

  if (event.type === 'failure') {
    const settled: SettledPersistenceState = {
      kind: 'failed',
      http_status: event.http_status,
      error_code: event.error_code,
      previous_clean: previousClean(runtime.settled),
    }
    // 失败必须立即可见；不能因为另一个请求仍在飞就把它粉饰成“保存中”。
    return { state: settled, settled, active_request_ids: active }
  }

  const settled = persistenceFromReceipt(event.receipt)
  return {
    state: active.length > 0 ? committing(settled, active) : settled,
    settled,
    active_request_ids: active,
  }
}
