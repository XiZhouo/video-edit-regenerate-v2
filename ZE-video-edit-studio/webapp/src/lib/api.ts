/**
 * api.ts — webapp ↔ 后端 (server.py) 的薄 HTTP/SSE 客户端(框架级 glue, 可复用)。
 *
 * 多项目 + 账号:所有数据请求带 `?project_id=`(当前打开项目)+ `Authorization: Bearer`
 * (token,经 authHeader)。SSE 用 EventSource 不能带 header → token 走 `?token=`；媒体 URL
 * 只携带短期 asset ticket，绝不复制主 session token。
 * 任意请求遇 401 → triggerAuthError(清登录态、踢回 LoginPage)。
 */

import type { EntityDict, RunPhase } from '@ze/core/04-user-facing/01-layout/03-editor'
export type { RunPhase } from '@ze/core/04-user-facing/01-layout/03-editor'
import type { CommandSpecBrief, PipelineRun, PipelineStaticMeta } from '@ze/core/04-user-facing/01-layout/02-footer'
import type { PersistenceReceipt, SavePolicy } from '@ze/core/04-user-facing/01-layout/persistenceState'
import { authHeader, getToken, triggerAuthError } from '@ze/core/04-user-facing/03-authentication'

const BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

const IDEM_SAFE = /[^A-Za-z0-9._:-]/g
const IDEM_SCOPE_MAX = 140

function shortHash(s: string): string {
  let h = 2166136261
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0).toString(36)
}

function compactIdempotencyScope(scope: string): string {
  const safe = scope.replace(IDEM_SAFE, '_')
  if (safe.length <= IDEM_SCOPE_MAX) return safe
  const suffix = `_${shortHash(safe)}`
  return `${safe.slice(0, IDEM_SCOPE_MAX - suffix.length)}${suffix}`
}

export function newIdempotencyKey(scope: string): string {
  const rand = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `web:${compactIdempotencyScope(scope)}:${rand}`
}

export type DocMeta = {
  id: string
  name: string
  schema_version: string
  collaboration_mode: 'live_collab' | 'with_save' | 'realtime_checkpoint'
  /** 声明策略可为空：null 表示旧文档未显式迁移。 */
  save_policy: SavePolicy | null
  /** 后端完成兼容映射后真正执行的策略；前端不得自行从 collaboration_mode 猜。 */
  effective_save_policy: SavePolicy
  auto_commit_enabled: boolean
  /** Project topology generation; reset/delete+recreate changes it even when id stays equal. */
  generation: number
  version: number
  data_schema: Record<string, unknown>
  acl?: { owner?: string; collaborators?: string[] }
  /** document 级 config(含 project_kind:前端据此识别 demo 沙盒态)*/
  config?: Record<string, unknown>
  /** R-C18:装载时若数据结构损坏, 摘要(finding_count);前端据此显示"结构诊断"入口。无损坏=null。 */
  schema_diagnosis?: { finding_count: number } | null
}

/** Schema Doctor(R-C18)诊断报告 —— 每处不符的可读修法 + AI 指令。 */
export type SchemaDoctorFinding = {
  path: string
  reason: string
  got: unknown
  got_type: string
  expected: Record<string, unknown>
  minimal_example: unknown
  how_to_fix: string
  ai_prompt: string
}
export type SchemaDoctorReport = {
  schema: string
  ok: boolean
  entity_type: string
  entity_id: string
  trigger: string
  finding_count: number
  findings: SchemaDoctorFinding[]
  minimal_valid_example?: unknown
  generated_at: string
}

/** 取结构诊断: 无参=全 doc 汇总, 传 type+id=单实体。只读, got 已服务端脱敏。 */
export async function fetchDiagnose(
  projectId: string, type?: string, id?: string,
): Promise<SchemaDoctorReport> {
  const q = new URLSearchParams({ project_id: projectId })
  if (type) q.set('type', type)
  if (id) q.set('id', id)
  const r = await fetch(`${BASE}/api/diagnose?${q.toString()}`, {
    credentials: 'include', cache: 'no-store', headers: { ...authHeader() },
  })
  if (!r.ok) throw await _toError(r)
  return r.json()
}

export type DocumentResponse = {
  meta: DocMeta
  entities: Record<string, EntityDict[]>
  /** Broker cursor captured atomically with this canonical snapshot. */
  event_cursor: number
  /** Incarnation of the process-local broker sequence domain. */
  event_epoch: string
}

/** 后端错误信封: {error:{code,message}}; HTTP status 体现语义 (409/404/422/403/401/400)。 */
export type ApiError = { status: number; code: string; message: string }

export async function getDocument(projectId: string): Promise<DocumentResponse> {
  const r = await fetch(`${BASE}/api/document?project_id=${encodeURIComponent(projectId)}`, {
    credentials: 'include',
    cache: 'no-store',
    headers: { ...authHeader() },
  })
  if (!r.ok) throw await _toError(r)
  return r.json()
}

export type CommandCatalog = {
  commands: CommandSpecBrief[]
  pipelines: PipelineStaticMeta[]
  project_generation: number
  document_version: number
}

export async function getCommands(projectId: string): Promise<CommandCatalog> {
  const r = await fetch(`${BASE}/api/commands?project_id=${encodeURIComponent(projectId)}`, {
    credentials: 'include',
    cache: 'no-store',
    headers: { ...authHeader() },
  })
  if (!r.ok) throw await _toError(r)
  const body = await r.json()
  return {
    commands: Array.isArray(body?.commands) ? body.commands : [],
    pipelines: Array.isArray(body?.pipelines) ? body.pipelines : [],
    project_generation: Number(body?.project_generation),
    document_version: Number(body?.document_version),
  }
}

// ── 引导精灵(Guided Tour;系统 3a 逻辑层的前端消费面)────────────────────────
/** 引导步封闭集(与 sdk/guided_tour.py TOUR_STEP_KINDS 同源)。 */
export type GuideStepKind = 'say' | 'point' | 'highlight' | 'act' | 'wait' | 'ask'
/** 单个引导步(投影自 key_flows;`presentation` 为前端呈现覆盖预留位)。 */
export type GuideStep = {
  id: string
  kind: GuideStepKind
  actor: string
  mode: 'guide' | 'act'
  say: string
  /** target 串:cmd:/entity:/control:/tool:/editor: 或呈现类 region:/guide:/vision:。 */
  target: string
  command_id: string | null
  confirm: boolean
  expected_effect: string
  presentation: Record<string, unknown>
}
export type GuideTour = {
  id: string
  title: string
  trigger: string
  when: string
  mode: 'guide' | 'act' | 'mixed'
  source: 'instance' | 'fallback'
  steps: GuideStep[]
  step_count: number
}

/** Help『教我用』本地播:拉全量 tour(含 steps)。summaries 已在 describe。 */
/** 读全局配置生效值快照(key → value;登录即可读,secret 已脱敏)。 */
export async function fetchConfigSnapshot(): Promise<Record<string, unknown>> {
  const r = await fetch(`${BASE}/api/config`, {
    credentials: 'include', cache: 'no-store', headers: { ...authHeader() },
  })
  if (!r.ok) throw await _toError(r)
  const body = await r.json()
  const items = Array.isArray(body?.config) ? (body.config as { key?: unknown; value?: unknown }[]) : []
  const out: Record<string, unknown> = {}
  for (const item of items) {
    if (typeof item?.key === 'string') out[item.key] = item.value
  }
  return out
}

/** 取一条 AI 待确认命令的批准材料(approval_nonce)。
 *  浏览器 cookie 专属端点:刻意**不带 Authorization**(服务端也会忽略它)——
 *  批准能力只属于持会话 cookie 的人,AI 子进程只有 Bearer token,到不了这里。 */
export async function fetchPendingApproval(
  projectId: string, token: string,
): Promise<{ approval_nonce: string; command_id: string; summary: string }> {
  const r = await fetch(
    `${BASE}/api/command/pending-approval?project_id=${encodeURIComponent(projectId)}&token=${encodeURIComponent(token)}`,
    { credentials: 'include', cache: 'no-store' },
  )
  if (!r.ok) throw await _toError(r)
  const body = await r.json()
  const pending = (body?.pending ?? {}) as Record<string, unknown>
  return {
    approval_nonce: String(pending.approval_nonce ?? ''),
    command_id: String(pending.command_id ?? ''),
    summary: String(pending.summary ?? ''),
  }
}

/** 批准执行一条待确认命令(既有人工 nonce 通道;执行结果经 SSE 实时到达)。 */
export async function confirmPendingCommand(
  projectId: string, token: string, approvalNonce: string, expectedGeneration: number,
): Promise<void> {
  const r = await fetch(
    `${BASE}/api/command/confirm?project_id=${encodeURIComponent(projectId)}`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...authHeader() },
      body: JSON.stringify({
        token, approval_nonce: approvalNonce, expected_generation: expectedGeneration,
      }),
    },
  )
  if (!r.ok) throw await _toError(r)
}

export async function fetchGuidedTours(projectId: string): Promise<GuideTour[]> {
  const r = await fetch(`${BASE}/api/context/tours?project_id=${encodeURIComponent(projectId)}`, {
    credentials: 'include',
    cache: 'no-store',
    headers: { ...authHeader() },
  })
  if (!r.ok) throw await _toError(r)
  const body = await r.json()
  return Array.isArray(body?.tours) ? (body.tours as GuideTour[]) : []
}

export type PendingCommandResult = { pending: Record<string, unknown> }
export type AppliedCommandResult = {
  op: Record<string, unknown>
  /** 一笔命令包含占位等派生 Operation 时，op 仍是主命令，ops 给出完整提交顺序。 */
  ops?: Record<string, unknown>[]
  /** 非 committed/事件构建失败时没有 canonical event，不能伪造 changes。 */
  changes?: ChangePayload | ChangePayload[]
  persistence: PersistenceReceipt
  actions?: Record<string, unknown>[]
  action_status?: 'succeeded' | 'failed' | 'skipped'
}
/** Every mutation response is fenced to the project generation that produced it. */
export type CommandResult = (AppliedCommandResult | PendingCommandResult) & {
  project_generation: number
}

export async function dispatch(
  commandId: string, args: Record<string, unknown>, projectId: string,
  idempotencyKey: string | undefined,
  expectedGeneration: number,
): Promise<CommandResult> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...authHeader() }
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  const q = `project_id=${encodeURIComponent(projectId)}`
  const r = await fetch(`${BASE}/api/command/${commandId}?${q}`, {
    method: 'POST',
    credentials: 'include',
    headers,
    body: JSON.stringify({ ...args, expected_generation: expectedGeneration }),
  })
  if (!r.ok) throw await _toError(r)
  return r.json()
}

/**
 * Rename a project that is not the currently rendered document.
 *
 * The project catalog intentionally carries only the topology generation, not
 * the document revision.  Read one authoritative target snapshot, then submit
 * exactly one mutation fenced by both axes.  A write between GET and POST is a
 * normal 409 and is deliberately returned to the caller; this helper never
 * refreshes the version and retries behind the user's back.
 */
export async function renameUnrenderedDocument(
  projectId: string,
  name: string,
  idempotencyKey: string,
): Promise<CommandResult> {
  const snapshot = await getDocument(projectId)
  const expectedVersion = snapshot.meta.version
  const expectedGeneration = snapshot.meta.generation
  if (!Number.isSafeInteger(expectedVersion) || expectedVersion <= 0
      || !Number.isSafeInteger(expectedGeneration) || expectedGeneration <= 0) {
    throw {
      status: 0,
      code: 'response_contract_invalid',
      message: 'target document snapshot has no valid CAS version',
    } satisfies ApiError
  }
  return dispatch(
    'document.rename',
    { name, expected_version: expectedVersion },
    projectId,
    idempotencyKey,
    expectedGeneration,
  )
}

/** undo/redo/history 共用的可用状态(can_undo / can_redo);其余字段(ops/entries)按需忽略。 */
export type HistoryState = {
  can_undo: boolean
  can_redo: boolean
  project_generation: number
  document_version: number
  ops?: Record<string, unknown>[]
  changes?: ChangePayload[]
}

/** undo/redo 属于 mutation，必须返回持久化回执；只读 getHistory 不带该字段。 */
export type HistoryMutationResult = HistoryState & {
  persistence: PersistenceReceipt
  project_generation: number
}

/** 撤销最末操作。entity 变更经 _apply_history → broker → SSE 回流(同 dispatch),
 *  这里只取 can_undo/can_redo 给按钮态;不手动 apply changes(否则与 SSE 双重落地)。 */
export async function undo(
  projectId: string, idempotencyKey: string | undefined, expectedGeneration: number,
): Promise<HistoryMutationResult> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...authHeader() }
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  const q = `project_id=${encodeURIComponent(projectId)}`
  const r = await fetch(`${BASE}/api/undo?${q}`, {
    method: 'POST', credentials: 'include', headers,
    body: JSON.stringify({ expected_generation: expectedGeneration }),
  })
  if (!r.ok) throw await _toError(r)
  return r.json()
}

/** 重做最后一次撤销(语义同 undo)。 */
export async function redo(
  projectId: string, idempotencyKey: string | undefined, expectedGeneration: number,
): Promise<HistoryMutationResult> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...authHeader() }
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  const q = `project_id=${encodeURIComponent(projectId)}`
  const r = await fetch(`${BASE}/api/redo?${q}`, {
    method: 'POST', credentials: 'include', headers,
    body: JSON.stringify({ expected_generation: expectedGeneration }),
  })
  if (!r.ok) throw await _toError(r)
  return r.json()
}

/** 读 undo/redo 可用状态(初次加载 / 他人操作经 SSE 回流后重建按钮态)。 */
export async function getHistory(projectId: string): Promise<HistoryState> {
  const r = await fetch(`${BASE}/api/history?project_id=${encodeURIComponent(projectId)}`, {
    credentials: 'include',
    cache: 'no-store',
    headers: { ...authHeader() },
  })
  if (!r.ok) throw await _toError(r)
  return r.json()
}

/** entity_change 广播 payload (见 _protocol/CHANGE-FLOW §4 / to_broadcast_payload)。 */
export type EntityChange = {
  entity_type: string
  entity_id: string
  change_kind: 'created' | 'updated' | 'soft_deleted' | 'restored' | 'hard_removed'
  changed_fields: string[]
  snapshot: EntityDict | null
}
export type ChangePayload = {
  type: 'entity_change'
  /** Present on SSE frames; HTTP command responses fence the payload at top level. */
  project_generation?: number
  seq?: number
  /** Canonical document version after this committed event. */
  document_version?: number
  doc_id: string
  op_id: string
  trace_id: string
  source: string
  actor: Record<string, unknown>
  ts: number
  command_id: string
  changes: EntityChange[]
}

/** 多 stage item 内单个 stage 的运行态(后端 StageStatus.to_dict;P2)。 */
export type StageStatusDict = {
  stage_id: string
  label: string
  phase: RunPhase
  progress: number | null
  eta_sec: number | null
  throughput: number | null
}

/** entity 运行态(占位 / 进度 / 错误)广播 payload。
 *  多 stage 字段(stages/stage_index/eta_sec)仅多 stage run 携带,单 stage 缺省。 */
export type EntityRunStatusDict = {
  entity_type: string
  entity_id: string
  phase: RunPhase
  progress: number | null
  run_id: string | null
  stage_label: string
  error: { code: string; message: string; trace_id?: string } | null
  updated_at: number
  stages?: StageStatusDict[]
  stage_index?: number
  eta_sec?: number
  placeholder?: boolean
}
export type StatusPayload = {
  type: 'entity_status'
  project_generation: number
  event_epoch: string
  /** Present for broker-delivered runtime frames; absent on connection snapshot. */
  seq?: number
  /** Initial/reconnect snapshot replaces the complete runtime-status map. */
  replace?: boolean
  doc_id: string | null
  run_id: string | null
  trace_id: string | null
  statuses: EntityRunStatusDict[]
}

/** 聚合 pipeline run 状态广播 payload;runs 形态对齐 footer PipelineRun。 */
export type PipelineStatusPayload = {
  type: 'pipeline_status'
  project_generation: number
  event_epoch: string
  /** Present for broker-delivered runtime frames; absent on connection snapshot. */
  seq?: number
  doc_id: string | null
  runs: PipelineRun[]
}

export type ProjectGenerationPayload = {
  type: 'project_generation'
  doc_id: string
  generation: number
  project_generation: number
  event_epoch: string
}

/** Server cannot replay from the requested cursor (buffer gap / process restart). */
export type ResyncRequiredPayload = {
  type: 'resync_required'
  project_generation: number
  event_epoch: string
  reason: string
  event_cursor?: number
}

export type AccessRevokedPayload = {
  type: 'access_revoked'
  project_generation: number
  event_epoch: string
  reason?: string
}

/** 引导精灵 SSE 帧(系统 3a 经既有广播通道流式;per-run 无 changes,只匹配 doc 级订阅)。
 *  仅承载引导可视化,不改 Document;GuideLayer 消费,useDocument 主流忽略。 */
export type GuideStepPayload = {
  type: 'guide_step'
  doc_id: string
  runId: string
  index: number
  total: number
  step: GuideStep
}
export type GuideEndPayload = {
  type: 'guide_end'
  doc_id: string
  runId: string
  status: string
}

/** AI 发起的需确认命令(确认拉起帧)。**不含批准材料**——approval_nonce 只经
 *  浏览器 cookie 专属端点 fetchPendingApproval 下发,AI 子进程结构上拿不到。 */
export type PendingConfirmationPayload = {
  type: 'pending_confirmation'
  doc_id: string
  token: string
  command_id: string
  source: string
  summary: string
  ts: number
  actor: { user_id: string }
  entity_ref: { entity_type: string; entity_id: string } | null
}

/** SSE 消息(同一通道,按 type 判别)。 */
export type SseChangePayload = ChangePayload & { project_generation: number; event_epoch: string }
/** §7.3 W1-4 助手轮阶段帧(呈现层):过程卡五段靠它逐段推进。
 *  不改 Document、不带 generation 栅栏 —— 与 guide_step 同性质,主流不处理只转投。
 *  tool 段带命令名,过程卡才说得出「正在调用 items.generate」而不是一句「处理中」。 */
export type AssistantStagePayload = {
  type: 'assistant_stage'
  doc_id: string
  turn_id: string
  /** 封闭集:understanding | reading | tool | waiting | done(sdk/assistant_turn.STAGES) */
  stage: string
  tool: string
}

export type SsePayload = SseChangePayload | StatusPayload | PipelineStatusPayload
  | ProjectGenerationPayload | ResyncRequiredPayload | AccessRevokedPayload
  | GuideStepPayload | GuideEndPayload | PendingConfirmationPayload
  | AssistantStagePayload

const SSE_TYPES = new Set([
  'entity_change', 'entity_status', 'pipeline_status', 'project_generation',
  'resync_required', 'access_revoked', 'guide_step', 'guide_end', 'pending_confirmation',
  // 漏了这一行 = 帧在入口就被丢掉,类型改对了过程卡照样永远不动(自查时实测到的)。
  'assistant_stage',
])

export type EventStreamOptions = {
  projectId: string
  eventEpoch: string
  sinceSeq?: number
  onPayload: (p: SsePayload) => void
  onOpen?: () => void
  onError?: () => void
  onClose?: () => void
}

/** 开 SSE(指定 project + token);每条已知 type 的 payload 回调。返回关闭函数。 */
export function openEventStream(options: EventStreamOptions): () => void {
  const since = Math.max(0, Math.floor(options.sinceSeq ?? 0))
  const q = [
    `project_id=${encodeURIComponent(options.projectId)}`,
    `token=${encodeURIComponent(getToken())}`,
    `event_epoch=${encodeURIComponent(options.eventEpoch)}`,
    ...(since > 0 ? [`since_seq=${encodeURIComponent(String(since))}`] : []),
  ].join('&')
  // Guest demo ownership is carried by the HttpOnly ze_guest cookie.  This
  // remains required when VITE_API_BASE points at a different dev origin.
  const es = new EventSource(`${BASE}/api/events?${q}`, { withCredentials: true })
  es.onopen = () => options.onOpen?.()
  es.onerror = () => options.onError?.()
  es.onmessage = (e) => {
    try {
      const p = JSON.parse(e.data) as SsePayload
      if (p && SSE_TYPES.has(p.type)) options.onPayload(p)
    } catch { /* 忽略非 JSON (keepalive 注释不会进 onmessage) */ }
  }
  return () => {
    es.close()
    options.onClose?.()
  }
}

/** asset 引用 → 短期媒体 URL；缺票据时 fail closed，不退回 session query token。 */
export function assetUrl(
  ref: { sha256: string }, projectId: string, generation: number, ticket: string | undefined,
): string {
  if (!ticket || !Number.isSafeInteger(generation) || generation <= 0) return ''
  const q = `project_id=${encodeURIComponent(projectId)}`
    + `&generation=${encodeURIComponent(String(generation))}`
    + `&ticket=${encodeURIComponent(ticket)}`
  return `${BASE}/api/asset/${ref.sha256}?${q}`
}

/** asset 引用形态(POST /api/asset 返回 ref.to_dict())。 */
export type AssetRefDict = { sha256: string; mime?: string; size?: number; ext?: string }

export type AssetTicketBatch = {
  tickets: Record<string, string>
  expires_at: number
  missing: string[]
  project_generation: number
}

/** 用正常 fetch/Bearer 批量换取短期、principal/project/generation/sha 绑定票据。 */
export async function requestAssetTickets(
  projectId: string,
  expectedGeneration: number,
  sha256s: string[],
  signal?: AbortSignal,
): Promise<AssetTicketBatch> {
  const r = await fetch(`${BASE}/api/asset-tickets?project_id=${encodeURIComponent(projectId)}`, {
    method: 'POST',
    credentials: 'include',
    cache: 'no-store',
    signal,
    headers: {
      'Content-Type': 'application/json',
      'X-Project-Generation': String(expectedGeneration),
      ...authHeader(),
    },
    body: JSON.stringify({ sha256s, expected_generation: expectedGeneration }),
  })
  if (!r.ok) throw await _toError(r)
  const body = await r.json() as Partial<AssetTicketBatch>
  const requested = new Set(sha256s)
  const ticketEntries = body.tickets && typeof body.tickets === 'object' && !Array.isArray(body.tickets)
    ? Object.entries(body.tickets)
    : []
  const ticketShas = new Set(ticketEntries.map(([sha]) => sha))
  const missing = Array.isArray(body.missing) ? body.missing : []
  const missingShas = new Set(missing)
  const responseShas = new Set([...ticketShas, ...missingShas])
  if (
    body.project_generation !== expectedGeneration
    || !Number.isSafeInteger(body.expires_at) || Number(body.expires_at) <= 0
    || !body.tickets || typeof body.tickets !== 'object' || Array.isArray(body.tickets)
    || !Array.isArray(body.missing)
    || ticketEntries.some(([sha, value]) => (
      !requested.has(sha) || typeof value !== 'string' || !value.startsWith('mt1.')
    ))
    || missing.some(sha => typeof sha !== 'string' || !requested.has(sha))
    || missingShas.size !== missing.length
    || [...ticketShas].some(sha => missingShas.has(sha))
    || responseShas.size !== requested.size
    || [...requested].some(sha => !responseShas.has(sha))
  ) {
    throw {
      status: 0,
      code: 'response_contract_invalid',
      message: 'asset ticket response is invalid',
    } satisfies ApiError
  }
  return body as AssetTicketBatch
}

/** 上传二进制 → 当前 project 的内容寻址 AssetStore,返回 asset_ref。
 *  唯一前端「写资产」接缝:mod 图元(W1a mask 提交 / 导图)拿 ref 后再 dispatch 命令(JSON)落库。
 *  (单次 raw-body POST;大文件分块/续传是后续 W2,不在此。) */
export async function uploadAsset(
  bytes: Uint8Array | ArrayBuffer, mime: string, ext: string, projectId: string,
  expectedGeneration: number,
): Promise<AssetRefDict> {
  const q = `project_id=${encodeURIComponent(projectId)}&ext=${encodeURIComponent(ext)}`
    + `&expected_generation=${encodeURIComponent(String(expectedGeneration))}`
  const body = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  const r = await fetch(`${BASE}/api/asset?${q}`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': mime || 'application/octet-stream',
      'X-Project-Generation': String(expectedGeneration),
      ...authHeader(),
    },
    body: body as BodyInit,
  })
  if (!r.ok) throw await _toError(r)
  return r.json()
}

async function downloadProjectFile(
  endpoint: 'manifest' | 'export', name: string, projectId: string, expectedGeneration: number,
): Promise<void> {
  const q = `project_id=${encodeURIComponent(projectId)}`
    + `&expected_generation=${encodeURIComponent(String(expectedGeneration))}`
  const r = await fetch(`${BASE}/api/${endpoint}/file/${encodeURIComponent(name)}?${q}`,
    {
      credentials: 'include',
      cache: 'no-store',
      headers: {
        'X-Project-Generation': String(expectedGeneration),
        ...authHeader(),
      },
    })
  if (!r.ok) throw await _toError(r)
  const blob = await r.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

/** 下载 .ze-pack 导出件(manifest.export 产名;member-gated 路由,经 fetch 带 token → blob 落地)。 */
export async function downloadManifestFile(
  name: string,
  projectId: string,
  expectedGeneration: number,
): Promise<void> {
  return downloadProjectFile('manifest', name, projectId, expectedGeneration)
}

/** 下载声明式 FormAction 产生的 project JSON export。端点固定，mod 不能提供 URL/path/MIME。 */
export async function downloadProjectExportFile(
  name: string,
  projectId: string,
  expectedGeneration: number,
): Promise<void> {
  return downloadProjectFile('export', name, projectId, expectedGeneration)
}

async function _toError(r: Response): Promise<ApiError> {
  if (r.status === 401) triggerAuthError()      // 未登录 / token 失效 → 踢回登录
  let code = 'http_error', message = r.statusText
  try {
    const body = await r.json()
    if (body?.error) { code = body.error.code; message = body.error.message }
  } catch { /* 非 JSON body */ }
  return { status: r.status, code, message }
}
