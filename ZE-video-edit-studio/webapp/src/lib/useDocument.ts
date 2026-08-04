/**
 * useDocument — webapp 的文档 provider hook(框架级 glue, 可复用)。
 *
 * 多项目:接收 projectId(当前打开的项目),所有请求/SSE 带该 project_id。
 *   - projectId 变 → effect cleanup 自动断旧 SSE + 重订;先清 meta/entities 防显示旧项目数据。
 *   - projectId=null(未打开项目)→ 不加载(中区显 ProjectPicker)。
 *   - 隔离靠后端 per-project broker(/api/events?project_id= 只推该项目变更)+ effect cleanup。
 *   - 409(版本冲突)→ 重新 getDocument 自愈。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { EntityDict, EntityRunStatus } from '@ze/core/04-user-facing/01-layout/03-editor'
import type { CommandSpecBrief, ErrorEntry, OpLogEntry, PipelineRun, PipelineStaticMeta, ServerStatus } from '@ze/core/04-user-facing/01-layout/02-footer'
import {
  createPersistenceRuntime,
  reducePersistence,
  type PersistenceEvent,
  type PersistenceReceipt,
  type PersistenceState,
} from '@ze/core/04-user-facing/01-layout/persistenceState'
import type { SyncState } from '@ze/core/04-user-facing/01-layout/syncState'
import {
  type ApiError, type ChangePayload, type DocMeta,
  type EntityRunStatusDict, type SsePayload, type CommandResult,
  dispatch as apiDispatch, getCommands, getDocument, openEventStream,
  undo as apiUndo, redo as apiRedo, getHistory,
  newIdempotencyKey,
} from './api'
import {
  type EventStreamStatus, type PendingSyncRecord, type SyncConflictRecord,
  deriveSyncState,
} from './syncState'
import { publishGuideFrame } from './guideBus'
import { publishAssistant } from '@ze/core/04-user-facing'
import { publishAiActivity } from './aiActivityBus'

type EntitiesByType = Record<string, EntityDict[]>
type EntityStatusMap = Record<string, EntityRunStatus>

const statusKey = (t: string, id: string) => `${t}/${id}`

function toRunStatus(d: EntityRunStatusDict): EntityRunStatus {
  return {
    phase: d.phase,
    progress: d.progress ?? undefined,
    stage_label: d.stage_label || undefined,
    error: d.error ?? undefined,
    run_id: d.run_id ?? undefined,
    // 多 stage 字段:单 stage payload 不带这些键 → 保持 undefined(消费方退回静态 stages)。
    // 显式逐字段映射:在边界把 null 归一成 undefined,且未来 wire 形态漂移会编译报错(非隐式结构转型)。
    stages: d.stages?.map((s) => ({
      stage_id: s.stage_id,
      label: s.label,
      phase: s.phase,
      progress: s.progress ?? undefined,
      eta_sec: s.eta_sec ?? undefined,
      throughput: s.throughput ?? undefined,
    })),
    stage_index: d.stage_index,
    eta_sec: d.eta_sec,
    placeholder: d.placeholder === true ? true : undefined,
  }
}

/** Apply either one runtime delta or an authoritative reconnect snapshot. */
export function reduceEntityStatusPayload(
  prev: EntityStatusMap,
  payload: Pick<Extract<SsePayload, { type: 'entity_status' }>, 'replace' | 'statuses'>,
): EntityStatusMap {
  const next: EntityStatusMap = payload.replace ? {} : { ...prev }
  for (const status of payload.statuses) {
    const key = statusKey(status.entity_type, status.entity_id)
    if (status.phase === 'done') delete next[key]
    else next[key] = toRunStatus(status)
  }
  return next
}

const DOCUMENT_TARGET = '__document__'

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  const obj = value as Record<string, unknown>
  return `{${Object.keys(obj).sort().map((k) => `${JSON.stringify(k)}:${stableStringify(obj[k])}`).join(',')}}`
}

function upsert(list: EntityDict[], e: EntityDict): EntityDict[] {
  const i = list.findIndex((x) => x.id === e.id)
  if (i === -1) return [...list, e]
  const next = list.slice()
  next[i] = e
  return next
}
function removeById(list: EntityDict[], id: string): EntityDict[] {
  return list.filter((x) => x.id !== id)
}

function payloadToOplog(p: ChangePayload): OpLogEntry {
  return {
    id: p.op_id,
    trace_id: p.trace_id,
    command_id: p.command_id,
    command_label: p.command_id,
    source: (p.source as OpLogEntry['source']) ?? 'user',
    status: 'success',
    duration_ms: 0,
    timestamp: (p.ts ?? Date.now() / 1000) * 1000,
  }
}

function appliedOpIds(result: unknown): string[] {
  const changes = changePayloadsFromResult(result)
  const body = result as {
    op?: { id?: unknown }
    ops?: Array<{ id?: unknown }>
    pending?: unknown
  } | null
  if (!body || typeof body !== 'object' || body.pending) return []
  const ids: string[] = []
  if (typeof body.op?.id === 'string' && body.op.id) ids.push(body.op.id)
  for (const op of body.ops ?? []) {
    if (typeof op?.id === 'string' && op.id) ids.push(op.id)
  }
  for (const p of changes) {
    if (typeof p?.op_id === 'string' && p.op_id) ids.push(p.op_id)
  }
  return [...new Set(ids)]
}

function changePayloadsFromResult(result: unknown): ChangePayload[] {
  if (!result || typeof result !== 'object') return []
  const body = result as {
    changes?: ChangePayload | ChangePayload[]
    pending?: unknown
  }
  if (body.pending) return []
  const changes = Array.isArray(body.changes)
    ? body.changes
    : body.changes
      ? [body.changes]
      : []
  return changes.filter((p): p is ChangePayload => Boolean(p && p.type === 'entity_change' && Array.isArray(p.changes)))
}

function postCommitIssue(result: unknown): ApiError | null {
  if (!result || typeof result !== 'object') return null
  const body = result as {
    action_status?: unknown
    actions?: Array<{ status?: unknown; code?: unknown; message?: unknown }>
    persistence?: PersistenceReceipt
  }
  const persistenceStatus = body.persistence?.status
  if (persistenceStatus === 'dirty' || persistenceStatus === 'aborted' || persistenceStatus === 'indeterminate') {
    return {
      status: 0,
      code: body.persistence?.failure?.code || `commit_${persistenceStatus}`,
      message: body.persistence?.failure?.message || `commit_${persistenceStatus}`,
    }
  }
  if (body.persistence?.event_status === 'failed') {
    return {
      status: 0,
      code: body.persistence.failure?.code || 'event_delivery_failed',
      message: body.persistence.failure?.message || 'event_delivery_failed',
    }
  }
  if (body.action_status === 'failed' || body.action_status === 'skipped') {
    const failed = body.actions?.find((action) => action.status === 'failed')
    return {
      status: 0,
      code: typeof failed?.code === 'string' ? failed.code : 'after_commit_action_incomplete',
      message: typeof failed?.message === 'string'
        ? failed.message
        : 'after_commit_action_incomplete',
    }
  }
  return null
}

/** Durable commit succeeded but its canonical event was not delivered. */
export function requiresCanonicalEventResync(result: unknown): boolean {
  if (!result || typeof result !== 'object') return false
  const persistence = (result as { persistence?: PersistenceReceipt }).persistence
  return persistence?.status === 'clean' && persistence.event_status === 'failed'
}

function applyEntityChangePayloads(prev: EntitiesByType, payloads: ChangePayload[]): EntitiesByType {
  let next = { ...prev }
  for (const p of payloads) {
    for (const ch of p.changes) {
      if (ch.entity_type === DOCUMENT_TARGET) continue
      const et = ch.entity_type
      const cur = next[et] ?? []
      if (ch.change_kind === 'hard_removed' || ch.change_kind === 'soft_deleted') {
        next[et] = removeById(cur, ch.entity_id)
      } else if (ch.snapshot) {
        next[et] = upsert(cur, ch.snapshot)
      }
    }
  }
  return next
}

function docMetaPatchFromPayloads(payloads: ChangePayload[]): { name?: string; acl?: DocMeta['acl'] } | null {
  for (const p of payloads) {
    const docChange = p.changes.find((c) => c.entity_type === DOCUMENT_TARGET && c.snapshot)
    if (docChange?.snapshot) return docChange.snapshot as unknown as { name?: string; acl?: DocMeta['acl'] }
  }
  return null
}

function prependOplog(prev: OpLogEntry[], payloads: ChangePayload[]): OpLogEntry[] {
  const entries = payloads.map(payloadToOplog)
  const ids = new Set(entries.map((e) => e.id))
  return [...entries, ...prev.filter((e) => !ids.has(e.id))].slice(0, 50)
}

export type ProjectGenerationRelation = 'invalid' | 'adopt' | 'same' | 'newer' | 'stale'

/** Pure generation ordering rule, exported so the no-rollback fence is regression-tested. */
export function classifyProjectGeneration(
  current: number,
  incoming: number,
): ProjectGenerationRelation {
  if (!Number.isSafeInteger(incoming) || incoming <= 0) return 'invalid'
  if (!Number.isSafeInteger(current) || current <= 0) return 'adopt'
  if (incoming === current) return 'same'
  return incoming > current ? 'newer' : 'stale'
}

export type EventSequenceRelation = 'invalid' | 'next' | 'duplicate' | 'gap'

/** Canonical entity events must be consumed without holes inside one generation. */
export function classifyEventSequence(lastSeq: number, incomingSeq: number): EventSequenceRelation {
  if (!Number.isSafeInteger(incomingSeq) || incomingSeq <= 0) return 'invalid'
  if (incomingSeq <= lastSeq) return 'duplicate'
  return incomingSeq === lastSeq + 1 ? 'next' : 'gap'
}

/** A same-generation snapshot may advance, but may never roll back either axis. */
export function isMonotonicSnapshot(
  currentVersion: number,
  currentCursor: number,
  incomingVersion: number,
  incomingCursor: number,
): boolean {
  return Number.isSafeInteger(incomingVersion) && incomingVersion >= 0
    && Number.isSafeInteger(incomingCursor) && incomingCursor >= 0
    && incomingVersion >= currentVersion
    && incomingCursor >= currentCursor
}

/** A broker restart opens a new cursor domain; version still may not roll back. */
export function isMonotonicSnapshotFence(
  currentEpoch: string,
  currentVersion: number,
  currentCursor: number,
  incomingEpoch: string,
  incomingVersion: number,
  incomingCursor: number,
): boolean {
  if (!incomingEpoch) return false
  if (currentEpoch && incomingEpoch !== currentEpoch) {
    return Number.isSafeInteger(incomingVersion) && incomingVersion >= currentVersion
      && Number.isSafeInteger(incomingCursor) && incomingCursor >= 0
  }
  return isMonotonicSnapshot(currentVersion, currentCursor, incomingVersion, incomingCursor)
}

/** HTTP op ids become synchronized only after those ids were observed on SSE. */
export function hasStreamAcknowledged(opIds: readonly string[], observed: ReadonlySet<string>): boolean {
  return opIds.length > 0 && opIds.every((opId) => observed.has(opId))
}

export function isDocumentNamespaceReady(
  activeProjectId: string | null,
  loadedProjectId: string | null,
  activeRevision: number,
  loadedRevision: number | null,
  metaGeneration: number | null,
  activeGeneration: number,
): boolean {
  return Boolean(
    activeProjectId
    && loadedProjectId === activeProjectId
    && loadedRevision === activeRevision
    && metaGeneration !== null
    && metaGeneration === activeGeneration,
  )
}

/** Async UI results may commit local state only into the namespace that started them. */
export function isCurrentRuntimeNamespace(
  currentNamespace: string | null,
  requestNamespace: string | null,
): boolean {
  return requestNamespace !== null && currentNamespace === requestNamespace
}

/** Current-canvas mutations use the generation actually rendered, never a newer list row. */
export function selectTargetExpectedGeneration(input: {
  targetProjectId: string
  currentProjectId: string | null
  documentReady: boolean
  renderedGeneration: number
  listedGeneration?: number
}): number | undefined {
  if (input.targetProjectId === input.currentProjectId && input.documentReady) {
    return input.renderedGeneration
  }
  return input.listedGeneration
}

const DOCUMENT_VERSION_CAS_COMMANDS = new Set([
  'document.rename',
  'document.set_acl',
])

/**
 * Document metadata mutations must carry the revision that the user actually
 * rendered. Keep a positive stale revision intact so the server can return a
 * real CAS conflict; never replace it with a newer ref value and silently turn
 * an old UI decision into a last-write-wins update.
 */
export function hasRequiredDocumentExpectedVersion(
  commandId: string,
  args: Readonly<Record<string, unknown>>,
): boolean {
  if (!DOCUMENT_VERSION_CAS_COMMANDS.has(commandId)) return true
  const version = args.expected_version
  return Number.isSafeInteger(version) && (version as number) > 0
}

export function useDocument(projectId: string | null, projectRevision = 0) {
  const [meta, setMeta] = useState<DocMeta | null>(null)
  const [loadedProjectId, setLoadedProjectId] = useState<string | null>(null)
  const [loadedProjectRevision, setLoadedProjectRevision] = useState<number | null>(null)
  const [entitiesByType, setEntitiesByType] = useState<EntitiesByType>({})
  const [oplog, setOplog] = useState<OpLogEntry[]>([])
  const [serverStatus, setServerStatus] = useState<ServerStatus>('connecting')
  const [entityStatus, setEntityStatus] = useState<EntityStatusMap>({})
  const [pipelineRuns, setPipelineRuns] = useState<PipelineRun[]>([])
  const [commandSpecs, setCommandSpecs] = useState<CommandSpecBrief[]>([])
  const [pipelineSpecs, setPipelineSpecs] = useState<PipelineStaticMeta[]>([])
  const [canUndo, setCanUndo] = useState(false)
  const [canRedo, setCanRedo] = useState(false)
  const [streamStatus, setStreamStatus] = useState<EventStreamStatus>('idle')
  // Same project id can be replaced in-place (demo reset).  This epoch restarts
  // every generation-bound reader and the EventSource without changing the id.
  const [generationEpoch, setGenerationEpoch] = useState(0)
  const [snapshotRevision, setSnapshotRevision] = useState(0)
  const [streamRestart, setStreamRestart] = useState(0)
  const [syncRevision, setSyncRevision] = useState(0)
  const [syncNow, setSyncNow] = useState(Date.now())
  const [syncConflicts, setSyncConflicts] = useState<SyncConflictRecord[]>([])
  // 保存状态与同步状态是两条独立状态机：这里只接受 Document meta + mutation HTTP 回执，
  // 不根据 SSE 是否追上来猜“已保存”。
  const [persistenceRuntime, setPersistenceRuntime] = useState(() => createPersistenceRuntime(null))
  // 当前 projectId 的文档明确不存在(404 project_not_found)→ 上层据此回 picker,避免「死状态」
  // (持久化的 projectId 指向已被删除的项目 — demo 沙盒被列表过滤、不经列表守卫,尤需此兜底)。
  const [notFound, setNotFound] = useState(false)
  const [accessRevoked, setAccessRevoked] = useState(false)
  // 命令失败可见通道:dispatch/undo/redo 抛错(校验拒绝 / 服务错)→ 进此表,喂 footer ERRORS + toast。
  // 409(版本冲突)走自愈重取,不进此表(非用户错误)。
  const [errors, setErrors] = useState<ErrorEntry[]>([])
  const errSeq = useRef(0)
  const refetchRef = useRef<() => void>(() => {})
  // history GET 合并:协作高频编辑时每条 entity_change 都要刷历史 → 至多 1 个在飞 + 1 个待补,
  // 收尾再补的那次反映最新后端态(顺带消除"POST 设态被滞后 GET 覆盖"的竞态)。
  const histInFlight = useRef(false)
  const histPending = useRef(false)
  const refreshHistoryRef = useRef<() => void>(() => {})
  const pendingSyncRef = useRef(new Map<string, PendingSyncRecord & { opIds: string[] }>())
  const observedOpIdsRef = useRef(new Set<string>())
  const lastSseSeqRef = useRef(0)
  const eventEpochRef = useRef('')
  const documentVersionRef = useRef(0)
  const projectGenerationRef = useRef(0)
  const activeProjectIdRef = useRef<string | null>(projectId)
  const activeProjectRevisionRef = useRef(projectRevision)
  const generationDomainEpochRef = useRef(0)
  const docRequestSeqRef = useRef(0)
  const historyRequestSeqRef = useRef(0)
  const commandRequestSeqRef = useRef(0)
  const streamInstanceRef = useRef(0)
  const resyncInFlightRef = useRef(false)
  const historyMutationInFlight = useRef<string | null>(null)
  const retryTimerRef = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null)
  activeProjectIdRef.current = projectId
  activeProjectRevisionRef.current = projectRevision
  const documentReady = isDocumentNamespaceReady(
    projectId,
    loadedProjectId,
    projectRevision,
    loadedProjectRevision,
    meta?.generation ?? null,
    projectGenerationRef.current,
  )
  const renderedDomainEpoch = generationDomainEpochRef.current
  const documentReadyRef = useRef(documentReady)
  documentReadyRef.current = documentReady

  const transitionPersistence = useCallback((event: PersistenceEvent) => {
    setPersistenceRuntime((prev) => reducePersistence(prev, event))
  }, [])

  const beginPersistence = useCallback((requestId: string) => {
    transitionPersistence({ type: 'begin', request_id: requestId })
  }, [transitionPersistence])

  const markPersistenceApplied = useCallback((requestId: string, result: unknown) => {
    const body = result as { pending?: unknown; persistence?: PersistenceReceipt } | null
    if (body?.pending) {
      transitionPersistence({ type: 'pending-confirmation', request_id: requestId })
      return
    }
    const persistence = body?.persistence
    transitionPersistence({ type: 'success', request_id: requestId, receipt: persistence })
    // document_version 是这次提交的 canonical 版本；meta 跟随回执，而不是靠 SyncState 推断。
    if (typeof persistence?.document_version === 'number') {
      const version = Math.max(documentVersionRef.current, persistence.document_version)
      documentVersionRef.current = version
      setMeta((prev) => prev && version > prev.version ? { ...prev, version } : prev)
    }
  }, [transitionPersistence])

  const markPersistenceError = useCallback((requestId: string, err: ApiError) => {
    transitionPersistence({
      type: 'failure',
      request_id: requestId,
      http_status: Number.isFinite(err?.status) ? err.status : 0,
      error_code: err?.code || 'persistence_failed',
    })
  }, [transitionPersistence])

  const bumpSync = useCallback(() => {
    setSyncRevision((v) => v + 1)
    setSyncNow(Date.now())
  }, [])

  const resetSyncRuntime = useCallback(() => {
    pendingSyncRef.current.clear()
    observedOpIdsRef.current.clear()
    lastSseSeqRef.current = 0
    setSyncConflicts([])
    setStreamStatus('idle')
    bumpSync()
  }, [bumpSync])

  const clearGenerationBoundState = useCallback((nextGeneration: number) => {
    generationDomainEpochRef.current += 1
    projectGenerationRef.current = nextGeneration
    docRequestSeqRef.current += 1
    historyRequestSeqRef.current += 1
    commandRequestSeqRef.current += 1
    streamInstanceRef.current += 1
    documentVersionRef.current = 0
    eventEpochRef.current = ''
    resyncInFlightRef.current = false
    documentReadyRef.current = false
    historyMutationInFlight.current = null
    if (retryTimerRef.current !== null) globalThis.clearTimeout(retryTimerRef.current)
    retryTimerRef.current = null
    setLoadedProjectId(null)
    setLoadedProjectRevision(null)
    setMeta(null)
    setEntitiesByType({})
    setOplog([])
    setEntityStatus({})
    setPipelineRuns([])
    setCommandSpecs([])
    setPipelineSpecs([])
    setCanUndo(false)
    setCanRedo(false)
    setNotFound(false)
    setAccessRevoked(false)
    setErrors([])
    setServerStatus('connecting')
    transitionPersistence({ type: 'reset', meta: null })
    resetSyncRuntime()
  }, [resetSyncRuntime, transitionPersistence])

  /** Close the current cursor domain and rebuild from a fresh canonical snapshot. */
  const requestResync = useCallback(() => {
    if (!activeProjectIdRef.current || resyncInFlightRef.current) return
    if (retryTimerRef.current !== null) globalThis.clearTimeout(retryTimerRef.current)
    retryTimerRef.current = null
    resyncInFlightRef.current = true
    documentReadyRef.current = false
    streamInstanceRef.current += 1
    docRequestSeqRef.current += 1
    setStreamStatus('connecting')
    setLoadedProjectId(null)
    setLoadedProjectRevision(null)
    setStreamRestart((value) => value + 1)
    queueMicrotask(() => refetchRef.current())
  }, [])

  /**
   * Observe a generation fence from GET/SSE/mutation.  A newer generation
   * invalidates the whole old domain; an older fence is simply stale evidence
   * and must never move the client backwards.
   */
  const observeProjectGeneration = useCallback((generation: number): boolean => {
    const current = projectGenerationRef.current
    const relation = classifyProjectGeneration(current, generation)
    if (relation === 'invalid' || relation === 'stale') return false
    if (relation === 'adopt') {
      projectGenerationRef.current = generation
      return true
    }
    if (relation === 'same') return true
    clearGenerationBoundState(generation)
    setGenerationEpoch((value) => value + 1)
    return false
  }, [clearGenerationBoundState])

  const abandonSync = useCallback((idempotencyKey: string) => {
    pendingSyncRef.current.delete(idempotencyKey)
    bumpSync()
  }, [bumpSync])

  const acceptMutationResult = useCallback((
    result: unknown,
    requestDomainEpoch: number,
    requestGeneration: number,
  ): boolean => {
    if (generationDomainEpochRef.current !== requestDomainEpoch) return false
    const generation = Number((result as { project_generation?: unknown } | null)?.project_generation)
    if (!Number.isSafeInteger(generation) || generation <= 0) {
      // A mixed-version server is unsafe here: refetch canonical state rather
      // than applying an unfenced response to whichever generation is visible.
      requestResync()
      return false
    }
    if (requestGeneration > 0 && generation !== requestGeneration) {
      observeProjectGeneration(generation)
      return false
    }
    if (!observeProjectGeneration(generation)) return false
    return generationDomainEpochRef.current === requestDomainEpoch
  }, [observeProjectGeneration, requestResync])

  const finishSyncedPending = useCallback((rec: PendingSyncRecord & { opIds: string[] }): boolean => {
    if (!rec.httpAck || rec.opIds.length === 0) return false
    rec.streamAck = hasStreamAcknowledged(rec.opIds, observedOpIdsRef.current)
    return rec.streamAck
  }, [])

  const beginSync = useCallback((idempotencyKey: string, commandId: string) => {
    if (!projectId) return
    pendingSyncRef.current.set(idempotencyKey, {
      idempotencyKey,
      commandId,
      startedAt: Date.now(),
      httpAck: false,
      streamAck: false,
      opIds: [],
    })
    setSyncConflicts([])
    bumpSync()
  }, [bumpSync, projectId])

  const markSyncApplied = useCallback((idempotencyKey: string, result: unknown) => {
    const rec = pendingSyncRef.current.get(idempotencyKey)
    if (!rec) return
    // HTTP proves commit/persistence only.  It must never impersonate the
    // stream acknowledgement: entity state and observed op ids are advanced
    // exclusively by a gap-checked SSE frame (or a fresh snapshot).
    const opIds = appliedOpIds(result)
    if (opIds.length === 0) {
      pendingSyncRef.current.delete(idempotencyKey)
      bumpSync()
      return
    }
    rec.httpAck = true
    rec.opIds = opIds
    if (finishSyncedPending(rec)) pendingSyncRef.current.delete(idempotencyKey)
    bumpSync()
  }, [bumpSync, finishSyncedPending])

  const markSyncError = useCallback((idempotencyKey: string, err: ApiError) => {
    pendingSyncRef.current.delete(idempotencyKey)
    if (err?.status === 409) {
      const traceId = err.code || `conflict-${Date.now()}`
      setSyncConflicts([{ traceId, at: Date.now() }])
    }
    bumpSync()
  }, [bumpSync])

  const markSseObserved = useCallback((opId: string | undefined) => {
    if (!opId) return
    observedOpIdsRef.current.add(opId)
    for (const [key, rec] of pendingSyncRef.current.entries()) {
      if (finishSyncedPending(rec)) pendingSyncRef.current.delete(key)
    }
    bumpSync()
  }, [bumpSync, finishSyncedPending])

  const refetch = useCallback(() => {
    if (!projectId) {
      docRequestSeqRef.current += 1
      setLoadedProjectId(null); setLoadedProjectRevision(null)
      setMeta(null); setEntitiesByType({}); setServerStatus('connecting')
      return
    }
    const requestProjectId = projectId
    const requestProjectRevision = projectRevision
    const requestSeq = ++docRequestSeqRef.current
    const requestDomainEpoch = generationDomainEpochRef.current
    getDocument(requestProjectId)
      .then((d) => {
        if (docRequestSeqRef.current !== requestSeq
            || activeProjectIdRef.current !== requestProjectId
            || activeProjectRevisionRef.current !== requestProjectRevision
            || generationDomainEpochRef.current !== requestDomainEpoch) return
        if (!observeProjectGeneration(d.meta.generation)) return
        if (generationDomainEpochRef.current !== requestDomainEpoch) return
        if (!isMonotonicSnapshotFence(
          eventEpochRef.current,
          documentVersionRef.current,
          lastSseSeqRef.current,
          d.event_epoch,
          d.meta.version,
          d.event_cursor,
        )) {
          // A GET that started before a same-generation commit/event may arrive
          // late.  It is evidence, not authority: keep the newer local axes and
          // ask again instead of rolling either one backwards.
          resyncInFlightRef.current = false
          requestResync()
          return
        }
        documentVersionRef.current = d.meta.version
        eventEpochRef.current = d.event_epoch
        lastSseSeqRef.current = d.event_cursor
        resyncInFlightRef.current = false
        if (retryTimerRef.current !== null) globalThis.clearTimeout(retryTimerRef.current)
        retryTimerRef.current = null
        pendingSyncRef.current.clear()
        observedOpIdsRef.current.clear()
        setSyncConflicts([])
        setLoadedProjectId(requestProjectId)
        setLoadedProjectRevision(requestProjectRevision)
        setMeta(d.meta); setEntitiesByType(d.entities); setServerStatus('online'); setNotFound(false)
        setStreamStatus('connecting')
        setSnapshotRevision((value) => value + 1)
        bumpSync()
        // 首次打开文档可确认 canonical 本身是 clean。错误后的自愈 refetch 只刷新内容，
        // 不得把刚被 409 拒绝的 mutation 改口成“已保存”。
        setPersistenceRuntime((prev) => prev.state.kind === 'unavailable'
          ? createPersistenceRuntime(d.meta)
          : prev)
        queueMicrotask(() => refreshHistoryRef.current())
      })
      .catch((e) => {
        if (docRequestSeqRef.current !== requestSeq
            || activeProjectIdRef.current !== requestProjectId
            || activeProjectRevisionRef.current !== requestProjectRevision
            || generationDomainEpochRef.current !== requestDomainEpoch) return
        setServerStatus('offline')
        // 明确「项目不存在」(404)才标记 notFound;瞬时网络错(status 0 等)不误判,避免误踢正常项目。
        const code = (e as { code?: string })?.code
        const status = (e as { status?: number })?.status
        const terminal = status === 404 || code === 'project_not_found'
        if (terminal) setNotFound(true)
        if (status === 403 || code === 'acl_denied') {
          clearGenerationBoundState(0)
          setAccessRevoked(true)
        }
        resyncInFlightRef.current = false
        if (!terminal && status !== 403 && code !== 'acl_denied') {
          retryTimerRef.current = globalThis.setTimeout(() => {
            retryTimerRef.current = null
            requestResync()
          }, 1000)
        }
      })
  }, [
    bumpSync, clearGenerationBoundState, observeProjectGeneration,
    projectId, projectRevision, requestResync,
  ])
  refetchRef.current = refetch

  // undo/redo 按钮态:初次加载 + 每条 entity_change 广播(含自己/他人操作)后重建。
  const refreshHistory = useCallback(() => {
    if (!projectId) {
      historyRequestSeqRef.current += 1
      setCanUndo(false); setCanRedo(false)
      return
    }
    if (histInFlight.current) { histPending.current = true; return }  // 合并并发请求
    histInFlight.current = true
    const requestProjectId = projectId
    const requestSeq = ++historyRequestSeqRef.current
    const requestDomainEpoch = generationDomainEpochRef.current
    getHistory(requestProjectId)
      .then((h) => {
        if (historyRequestSeqRef.current !== requestSeq
            || activeProjectIdRef.current !== requestProjectId
            || generationDomainEpochRef.current !== requestDomainEpoch) return
        if (h.project_generation !== projectGenerationRef.current
            || h.document_version < documentVersionRef.current) return
        setCanUndo(h.can_undo); setCanRedo(h.can_redo)
      })
      .catch(() => { /* 历史不可用不致命:按钮保持当前态 */ })
      .finally(() => {
        histInFlight.current = false
        if (histPending.current) { histPending.current = false; refreshHistoryRef.current() }  // 飞行中来过请求 → 补一次取最新
      })
  }, [projectId])
  refreshHistoryRef.current = refreshHistory

  // projectId 或显式 same-id reset revision 变:先清旧代数据(防串),再 load。
  useEffect(() => {
    clearGenerationBoundState(0)
    refetch()
  }, [clearGenerationBoundState, projectId, projectRevision, refetch])

  useEffect(() => () => {
    if (retryTimerRef.current !== null) globalThis.clearTimeout(retryTimerRef.current)
    retryTimerRef.current = null
  }, [])

  // Cross-tab/cross-device reset is first observed through an HTTP/SSE fence.
  // Restart all bounded reads after the old domain has been cleared.
  useEffect(() => {
    if (generationEpoch <= 0) return
    refetchRef.current()
  }, [generationEpoch])

  useEffect(() => {
    if (!projectId || !documentReady) return
    let alive = true
    const requestProjectId = projectId
    const requestSeq = ++commandRequestSeqRef.current
    const requestDomainEpoch = generationDomainEpochRef.current
    getCommands(projectId)
      .then((catalog) => {
        if (!alive || activeProjectIdRef.current !== requestProjectId
            || commandRequestSeqRef.current !== requestSeq
            || generationDomainEpochRef.current !== requestDomainEpoch) return
        if (catalog.project_generation !== projectGenerationRef.current
            || catalog.document_version < documentVersionRef.current) return
        setCommandSpecs(catalog.commands)
        setPipelineSpecs(catalog.pipelines)
      })
      .catch(() => {
        if (!alive || activeProjectIdRef.current !== requestProjectId
            || commandRequestSeqRef.current !== requestSeq
            || generationDomainEpochRef.current !== requestDomainEpoch) return
        setCommandSpecs([])
        setPipelineSpecs([])
      })
    return () => { alive = false }
  }, [documentReady, generationEpoch, projectId, projectRevision, snapshotRevision])

  // SSE belongs to one exact project generation.  projectRevision/generationEpoch
  // both rebuild the URL.  A stream starts only after GET supplied its atomic
  // cursor, then requests the broker's inclusive replay from cursor + 1.
  useEffect(() => {
    if (!projectId || !documentReady) return
    setStreamStatus('connecting')
    const streamProjectId = projectId
    const streamProjectRevision = projectRevision
    const streamDomainEpoch = generationDomainEpochRef.current
    const streamInstance = ++streamInstanceRef.current
    const isCurrentStream = () => (
      activeProjectIdRef.current === streamProjectId
      && activeProjectRevisionRef.current === streamProjectRevision
      && generationDomainEpochRef.current === streamDomainEpoch
      && streamInstanceRef.current === streamInstance
    )
    const close = openEventStream({
      projectId,
      eventEpoch: eventEpochRef.current,
      sinceSeq: lastSseSeqRef.current + 1,
      onOpen: () => {
        if (!isCurrentStream()) return
        setStreamStatus('open'); bumpSync()
      },
      onError: () => {
        if (!isCurrentStream()) return
        setStreamStatus('error'); bumpSync(); requestResync()
      },
      onClose: () => {
        if (!isCurrentStream()) return
        setStreamStatus('idle'); bumpSync()
      },
      onPayload: (p: SsePayload) => {
        if (!isCurrentStream()) return
        // 引导帧(呈现层,不改 Document、不带 generation 栅栏)→ 转投引导总线,主流不处理。
        if (p.type === 'guide_step' || p.type === 'guide_end') {
          publishGuideFrame(p)
          return
        }
        // §7.3 W1-4 阶段帧(呈现层)→ 转投助手总线,过程卡据此逐段推进。主流不处理。
        // 必须走推送:/api/ai/chat 是一次 POST,结果回来时这轮已结束,那时再说
        // 「刚才在读取」没有意义 —— 五段里前四段全是过程。
        if (p.type === 'assistant_stage') {
          publishAssistant({
            type: 'stage',
            stage: String((p as { stage?: unknown }).stage ?? ''),
            tool: String((p as { tool?: unknown }).tool ?? ''),
          })
          return
        }
        // 确认拉起帧(呈现层;帧不含批准材料)→ 转投 AI 行为总线,主流不处理。
        if (p.type === 'pending_confirmation') {
          publishAiActivity({ kind: 'pending', payload: p })
          return
        }
        if (p.type === 'access_revoked') {
          const relation = classifyProjectGeneration(
            projectGenerationRef.current,
            p.project_generation,
          )
          if (relation === 'invalid' || relation === 'stale') return
          clearGenerationBoundState(0)
          setAccessRevoked(true)
          return
        }
        if (p.type === 'resync_required') {
          requestResync()
          return
        }
        if (!p.event_epoch || p.event_epoch !== eventEpochRef.current) {
          requestResync()
          return
        }
        if (p.type === 'project_generation') {
          if (p.project_generation !== p.generation) {
            requestResync()
            return
          }
          observeProjectGeneration(p.generation)
          return
        }
        // Every data/status frame is stamped by the Context that emitted it.
        // A queue race may deliver G1 after G2 reset; the fence makes that frame
        // harmless even before the old EventSource notices its generation died.
        if (classifyProjectGeneration(
          projectGenerationRef.current,
          p.project_generation,
        ) === 'invalid') {
          requestResync()
          return
        }
        if (!observeProjectGeneration(p.project_generation)) return
        if (!isCurrentStream()) return
        if (p.type === 'entity_status' || p.type === 'pipeline_status') {
          if (p.seq !== undefined) {
            const sequence = classifyEventSequence(lastSseSeqRef.current, Number(p.seq))
            if (sequence === 'duplicate') return
            if (sequence !== 'next') {
              requestResync()
              return
            }
            lastSseSeqRef.current = p.seq
          }
        }
        if (p.type === 'entity_status') {
          setEntityStatus((prev) => reduceEntityStatusPayload(prev, p))
          publishAiActivity({ kind: 'entity_status', payload: p })   // 精灵起止回执用(纯呈现)
          return
        }
        if (p.type === 'pipeline_status') { setPipelineRuns(p.runs); return }
        const sequence = classifyEventSequence(lastSseSeqRef.current, Number(p.seq))
        if (sequence === 'duplicate') return
        if (sequence !== 'next') {
          requestResync()
          return
        }
        const incomingVersion = Number(p.document_version)
        if (!Number.isSafeInteger(incomingVersion) || incomingVersion < 0) {
          requestResync()
          return
        }
        lastSseSeqRef.current = p.seq as number
        documentVersionRef.current = Math.max(documentVersionRef.current, incomingVersion)
        markSseObserved(p.op_id)

        // entity_change:文档级(__document__)→ 更新 meta(name + acl);其余 → entities
        const snap = docMetaPatchFromPayloads([p])
        if (snap) setMeta((prev) => (prev ? {
          ...prev,
          name: snap.name ?? prev.name,
          acl: snap.acl ?? prev.acl,
          version: Math.max(prev.version, incomingVersion),
        } : prev))
        else setMeta((prev) => prev && incomingVersion > prev.version
          ? { ...prev, version: incomingVersion }
          : prev)
        setEntitiesByType((prev) => applyEntityChangePayloads(prev, [p]))
        setOplog((prev) => prependOplog(prev, [p]))
        refreshHistory()   // 操作发生(本人或协作者)→ 重建 undo/redo 可用态
        // AI 改动回执(纯呈现:精灵飞过去点/圈 + F2 高亮;归并在 useAiActivity)
        if (p.source === 'ai') publishAiActivity({ kind: 'ai_change', payload: p })
      },
    })
    return () => {
      if (streamInstanceRef.current === streamInstance) streamInstanceRef.current += 1
      close()
    }
  }, [
    bumpSync, clearGenerationBoundState, documentReady, generationEpoch, markSseObserved, observeProjectGeneration,
    projectId, projectRevision, refreshHistory, requestResync, snapshotRevision, streamRestart,
  ])

  useEffect(() => {
    if (pendingSyncRef.current.size === 0) return
    const id = window.setInterval(() => setSyncNow(Date.now()), 500)
    return () => window.clearInterval(id)
  }, [syncRevision])

  const syncState: SyncState = useMemo(() => deriveSyncState({
    projectId,
    serverStatus,
    streamStatus,
    pending: [...pendingSyncRef.current.values()],
    conflicts: syncConflicts,
    now: syncNow,
  }), [projectId, serverStatus, streamStatus, syncConflicts, syncNow, syncRevision])

  // Generic external errors (project-list actions, uploads, etc.) are visible
  // but must not mutate the currently open document's sync domain.
  const pushError = useCallback((e: ApiError) => {
    errSeq.current += 1
    const entry: ErrorEntry = {
      id: `cmd-err-${errSeq.current}`,
      trace_id: e?.code || 'cmd_error',
      level: 'error',
      message: e?.message || 'Command failed',
      timestamp: Date.now(),
    }
    setErrors((prev) => [entry, ...prev].slice(0, 50))
  }, [])

  // Errors from this hook's generation-bound write path may invalidate its
  // snapshot.  Only those 409s drive canonical resync.
  const pushDocumentError = useCallback((e: ApiError) => {
    if (e?.status === 409) {
      setSyncConflicts([{ traceId: e.code || `conflict-${Date.now()}`, at: Date.now() }])
      bumpSync()
      requestResync()
      return
    }
    pushError(e)
  }, [bumpSync, pushError, requestResync])

  const dismissError = useCallback((id: string) => {
    setErrors((prev) => prev.filter((e) => e.id !== id))
  }, [])

  const dispatchInFlight = useRef(new Map<string, Promise<CommandResult | undefined>>())

  const dispatchCmd = useCallback(async (
    commandId: string,
    args: Record<string, unknown>,
    options?: { idempotencyKey?: string },
  ) => {
    if (!projectId || !documentReadyRef.current
        || activeProjectRevisionRef.current !== projectRevision
        || generationDomainEpochRef.current !== renderedDomainEpoch) return
    if (!hasRequiredDocumentExpectedVersion(commandId, args)) {
      // Fail closed before opening an idempotency flight. This is a wiring
      // defect, not permission to substitute documentVersionRef.current: the
      // caller must send the version attached to the UI decision it rendered.
      pushDocumentError({
        status: 409,
        code: 'document_version_required',
        message: `${commandId} requires the rendered document expected_version`,
      })
      return
    }
    const explicitIdem = options?.idempotencyKey ?? ''
    const requestDomainEpoch = generationDomainEpochRef.current
    const requestGeneration = projectGenerationRef.current
    if (requestGeneration <= 0) return
    const requestProjectRevision = projectRevision
    const flightKey = `${projectId}\0${requestProjectRevision}\0${requestDomainEpoch}\0${requestGeneration}\0${commandId}\0${explicitIdem}\0${stableStringify(args)}`
    const existing = dispatchInFlight.current.get(flightKey)
    if (existing) return existing
    const idem = explicitIdem || newIdempotencyKey(`${projectId}:${commandId}`)
    const p = (async () => {
      beginSync(idem, commandId)
      beginPersistence(idem)
      try {
        // 返回 CommandResult(加性):结果型命令(manifest.export 的 external.target_ref)caller 可取用
        const result = await apiDispatch(commandId, args, projectId, idem, requestGeneration)
        if (!acceptMutationResult(result, requestDomainEpoch, requestGeneration)) {
          abandonSync(idem)
          transitionPersistence({ type: 'reset', meta: null })
          return undefined
        }
        markSyncApplied(idem, result)
        markPersistenceApplied(idem, result)
        const issue = postCommitIssue(result)
        if (issue) pushDocumentError(issue)
        if (requiresCanonicalEventResync(result)) requestResync()
        return result
      } catch (err) {
        if (generationDomainEpochRef.current !== requestDomainEpoch
            || activeProjectIdRef.current !== projectId
            || activeProjectRevisionRef.current !== requestProjectRevision) {
          abandonSync(idem)
          return undefined
        }
        markSyncError(idem, err as ApiError)
        markPersistenceError(idem, err as ApiError)
        pushDocumentError(err as ApiError)   // 校验拒绝 / 服务错 → 用户可见;409 走自愈
        return undefined
      }
    })()
    dispatchInFlight.current.set(flightKey, p)
    const clearFlight = () => {
      if (dispatchInFlight.current.get(flightKey) === p) dispatchInFlight.current.delete(flightKey)
    }
    void p.then(clearFlight, clearFlight)
    return p
  }, [
    abandonSync, acceptMutationResult, beginPersistence, beginSync,
    markPersistenceApplied, markPersistenceError, markSyncApplied, markSyncError,
    projectId, projectRevision, pushDocumentError, renderedDomainEpoch,
    requestResync, transitionPersistence,
  ])

  // Undo and redo share one gate.  Opposite-direction clicks must not race two
  // history cursors even though they use different HTTP routes.
  const mutateHistory = useCallback(async (kind: 'undo' | 'redo') => {
    if (!projectId || !documentReadyRef.current
        || activeProjectRevisionRef.current !== projectRevision
        || generationDomainEpochRef.current !== renderedDomainEpoch) return
    const requestDomainEpoch = generationDomainEpochRef.current
    const requestGeneration = projectGenerationRef.current
    if (requestGeneration <= 0) return
    const requestProjectRevision = projectRevision
    const flight = `${projectId}:${requestProjectRevision}:${requestDomainEpoch}:${requestGeneration}`
    if (historyMutationInFlight.current !== null) return
    historyMutationInFlight.current = flight
    const idem = newIdempotencyKey(`${projectId}:${kind}`)
    beginSync(idem, `history.${kind}`)
    beginPersistence(idem)
    try {
      const h = await (kind === 'undo'
        ? apiUndo(projectId, idem, requestGeneration)
        : apiRedo(projectId, idem, requestGeneration))
      if (!acceptMutationResult(h, requestDomainEpoch, requestGeneration)) {
        abandonSync(idem)
        transitionPersistence({ type: 'reset', meta: null })
        return
      }
      markSyncApplied(idem, h)
      markPersistenceApplied(idem, h)
      const issue = postCommitIssue(h)
      if (issue) pushDocumentError(issue)
      if (requiresCanonicalEventResync(h)) requestResync()
      // The response may be older than an intervening collaborator commit.
      // Rebuild history through its generation/version-fenced read instead of
      // assigning canUndo/canRedo directly from the mutation response.
      refreshHistoryRef.current()
    } catch (err) {
      if (generationDomainEpochRef.current !== requestDomainEpoch
          || activeProjectIdRef.current !== projectId
          || activeProjectRevisionRef.current !== requestProjectRevision) {
        abandonSync(idem)
        return
      }
      markSyncError(idem, err as ApiError)
      markPersistenceError(idem, err as ApiError)
      pushDocumentError(err as ApiError)
    } finally {
      if (historyMutationInFlight.current === flight) historyMutationInFlight.current = null
    }
  }, [
    abandonSync, acceptMutationResult, beginPersistence, beginSync,
    markPersistenceApplied, markPersistenceError, markSyncApplied, markSyncError,
    projectId, projectRevision, pushDocumentError, renderedDomainEpoch,
    requestResync, transitionPersistence,
  ])
  const undo = useCallback(() => mutateHistory('undo'), [mutateHistory])
  const redo = useCallback(() => mutateHistory('redo'), [mutateHistory])

  const statusFor = useCallback(
    (entityType: string, entityId: string): EntityRunStatus | undefined =>
      entityStatus[statusKey(entityType, entityId)],
    [entityStatus],
  )

  const visibleMeta = documentReady ? meta : null
  const visibleEntitiesByType = documentReady ? entitiesByType : {}
  const visibleOplog = documentReady ? oplog : []
  const visiblePipelineRuns = documentReady ? pipelineRuns : []
  const visibleCommandSpecs = documentReady ? commandSpecs : []
  const visiblePipelineSpecs = documentReady ? pipelineSpecs : []
  const persistenceState: PersistenceState = documentReady
    ? persistenceRuntime.state
    : { kind: 'unavailable' }

  return {
    meta: visibleMeta, entitiesByType: visibleEntitiesByType, oplog: visibleOplog, serverStatus, dispatchCmd, refetch,
    syncState, persistenceState,
    pipelineRuns: visiblePipelineRuns, commandSpecs: visibleCommandSpecs, pipelineSpecs: visiblePipelineSpecs, statusFor,
    canUndo: documentReady ? canUndo : false, canRedo: documentReady ? canRedo : false, undo, redo, notFound,
    errors, dismissError, pushError, accessRevoked,
    documentReady,
    projectGeneration: documentReady ? projectGenerationRef.current : 0,
    namespaceKey: documentReady ? `${projectId}:${projectGenerationRef.current}` : null,
  }
}
