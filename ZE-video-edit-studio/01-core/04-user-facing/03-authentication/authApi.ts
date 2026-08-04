/**
 * authApi.ts — 账号 / 项目 REST 客户端(薄封装 + token 管理)。
 *
 * token 存 localStorage(ze.auth.token);所有请求带 `Authorization: Bearer`。
 * 任意请求遇 401 → 触发全局 onAuthError(AuthProvider 注册)→ 清 token、踢回登录页。
 * 后端路由见 server.py:/api/auth/{lark/sse,logout,me,users}、/api/projects。
 *
 * 这是 core 能力的前端侧(与同目录 auth.py 后端对端);webapp 经
 * `@ze/core/04-user-facing/03-authentication` 桶导出引用。
 */

import { getT } from '@ze/i18n'

const BASE: string = ((import.meta as { env?: { VITE_API_BASE?: string } }).env?.VITE_API_BASE) ?? ''
const TOKEN_KEY = 'ze.auth.token'

// none = 尚未授权(可申请);ban = 明确封禁(申请也没用)。两者都不放行。
export type AuthPermission = 'none' | 'read' | 'edit' | 'ban'
export type AuthUser = {
  id: string
  name: string
  email?: string
  avatar_url?: string
  status?: 'active' | 'disabled'
  permission?: AuthPermission
  tool_id?: string
  is_super_admin?: boolean
}
export type AuthMethod = 'local' | 'feishu'
export type AuthRuntimeConfig = {
  schema_version: 'ze.auth-runtime-config.v1'
  mode: 'minimal-local' | 'federated' | 'hybrid'
  methods: AuthMethod[]
  preferred_method: AuthMethod
  registration: 'open' | 'closed'
  login_name_format: 'username' | 'email'
  email_verification_required: false
}
export type ZeUserCard = {
  id: string
  name: string
  email?: string | null
  avatar_url?: string | null
  status: 'active' | 'disabled'
  permission?: AuthPermission
  tool_id?: string
  first_seen_at?: number
  last_seen_at?: number
  last_login_at?: number
  last_login_tool?: string
  channels?: Record<string, boolean>
}
export type ProjectSummary = {
  id: string
  /** Topology generation observed in the same lease as this summary. */
  generation: number
  name: string
  owner: string
  collaborators: string[]
  updated_at: number
  is_owner: boolean
  /** R-C17:本项目是否已关联 git 仓库(服务端 sidecar 存在性;项目卡徽标用)。 */
  gitLinked: boolean
}
export type ProjectMutationStatus = {
  project_id: string
  intent_id: string
  status: 'committed' | 'deleted' | 'in_progress' | 'repair_required' | 'not_committed' | 'conflict'
  kind: ProjectTopologyIntentKind
  generation?: number
  phase?: string
  proof_revision: 2
  /** The server evaluated every found record in this authenticated principal scope. */
  principal_scoped: true
  /** A durable claim/journal/outcome currently reserves this exact intent tuple. */
  intent_reserved: boolean
}
export type ProjectTopologyIntentKind = 'create' | 'duplicate' | 'delete' | 'replace'
export type ProjectMutationCommit = {
  projectId: string
  generation: number
  kind: ProjectTopologyIntentKind
}
/** Immutable authentication snapshot for one business-intent runner. */
export type AuthRequestContext = { authToken: string }
type StableProjectMutationInput = {
  /** 32 lowercase hex business intent, stable across every retry. */
  intentId: string
  /** Opaque HTTP replay key, stable across every retry. */
  idempotencyKey: string
}
export type CreateProjectInput = StableProjectMutationInput & {
  targetProjectId: string
  name: string
  seed: boolean
}
export type DuplicateProjectInput = StableProjectMutationInput & {
  sourceProjectId: string
  expectedGeneration: number
  targetProjectId: string
  name?: string
}
export type DeleteProjectInput = StableProjectMutationInput & {
  targetProjectId: string
  expectedGeneration: number
  confirmName: string
}
export type OpenDemoTemplateInput = StableProjectMutationInput & {
  templateId: string
  sandboxProjectId: string
  /** Explicit CAS: 0 means first open; >0 means user-confirmed destructive reset. */
  expectedGeneration: number
}
export type CredentialProvider =
  | 'hupo_gateway_key'
  | 'openai_api_key'
  | 'anthropic_api_key'
  | 'cursor_api_key'
  | 'gemini_api_key'
  | 'codex_cli_profile'
  | 'claude_cli_profile'
  | 'github_repo'
export type CredentialProviderInfo = {
  id: CredentialProvider
  label: string
  help: string
}
export type CredentialRecord = {
  id: string
  provider: CredentialProvider
  label: string
  capabilities: string[]
  status: 'active' | 'needs_secret' | 'disabled' | 'needs_reauth'
  metadata: Record<string, unknown>
  last4: string
  has_secret: boolean
  created_at: number
  updated_at: number
  last_verified_at: number
  last_used_at: number
}
export type ModelScope = 'llm' | 'image' | 'video' | 'voice' | 'tts' | 'stt' | 'embedding'
export type AccountModelPolicy = {
  tool_id: string
  permission: AuthPermission
  model_scopes: Record<ModelScope, boolean>
  credentials: Record<string, { configured: boolean; credentials: CredentialRecord[] }>
}

// 走 globalThis.localStorage 而不是裸 localStorage:裸引用在没有 DOM 的环境(node 下的
// 组件测试)直接抛 ReferenceError,而调用链上游的 .catch 会把它吞成「加载完但没数据」——
// 组件停在 aria-busy 空壳,看起来像组件写错了,归因极难。本轮真踩过一次。
// 浏览器里 globalThis.localStorage 与 localStorage 完全等价,无行为变化。
function _store(): Storage | null {
  const s = (globalThis as { localStorage?: Storage }).localStorage
  return s ?? null
}
export function getToken(): string { return _store()?.getItem(TOKEN_KEY) ?? '' }
export function setToken(t: string): void { _store()?.setItem(TOKEN_KEY, t) }
export function clearToken(): void { _store()?.removeItem(TOKEN_KEY) }

/** 所有 fetch 注入(api.ts 也用此带 token)。 */
export function authHeader(tokenSnapshot?: string): Record<string, string> {
  const t = tokenSnapshot === undefined ? getToken() : tokenSnapshot
  return t ? { Authorization: `Bearer ${t}` } : {}
}

// 401 全局处理:AuthProvider 启动时 setAuthErrorHandler(onAuthError)。
let _onAuthError: (() => void) | null = null
export function setAuthErrorHandler(fn: (() => void) | null): void { _onAuthError = fn }
/** 供 api.ts 在自身请求收到 401 时触发全局踢登录(authApi 内部请求已内置)。 */
export function triggerAuthError(): void { _onAuthError?.() }

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

export function newAuthIdempotencyKey(scope: string): string {
  const rand = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `web:${compactIdempotencyScope(scope)}:${rand}`
}

export type AuthApiError = Error & {
  status: number
  code: string
  context?: Record<string, unknown>
}

function _err(
  status: number,
  code: string,
  message: string,
  context?: Record<string, unknown>,
): AuthApiError {
  const e = new Error(message) as AuthApiError
  e.status = status
  e.code = code
  if (context) e.context = context
  return e
}

async function _request(
  method: string,
  path: string,
  body?: unknown,
  options?: {
    idempotencyKey?: string
    /** A malformed HTTP response is outcome-unknown for business mutations. */
    responseContractErrorCode?: string
    responseContractMessage?: string
    /** Bind follow-up requests to the account that created the business intent. */
    tokenSnapshot?: string
  },
): Promise<any> {
  const headers: Record<string, string> = { ...authHeader(options?.tokenSnapshot) }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (options?.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey
  let r: Response
  try {
    r = await fetch(`${BASE}${path}`, {
      method,
      headers,
      credentials: 'include',
      cache: method === 'GET' ? 'no-store' : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    throw _err(0, 'network_error', getT()('auth.connectFail', { msg: (e as Error).message }))
  }
  if (!r.ok) {
    let code = `http_${r.status}`
    let message = r.statusText
    let context: Record<string, unknown> | undefined
    let hasErrorEnvelope = false
    try {
      const b = await r.json()
      if (b?.error
          && typeof b.error.code === 'string' && b.error.code
          && typeof b.error.message === 'string' && b.error.message) {
        code = b.error.code
        message = b.error.message
        hasErrorEnvelope = true
        context = (
          b.error.context && typeof b.error.context === 'object'
            ? b.error.context
            : undefined
        )
      }
    } catch { /* 非 JSON body */ }
    // Authentication UI reaction is independent from mutation-outcome proof.
    // Parse and preserve the server envelope first; never synthesize a
    // definitive `auth_required` that could retire an uncertain intent.
    if (r.status === 401 && !path.startsWith('/api/auth/')) {
      // A late response from account A must never sign out active account B.
      if (options?.tokenSnapshot === undefined || getToken() === options.tokenSnapshot) {
        _onAuthError?.()
      }
    }
    if (options?.responseContractErrorCode && !hasErrorEnvelope) {
      throw _err(
        502,
        options.responseContractErrorCode,
        options.responseContractMessage || getT()('auth.projectMutation.responseUnverifiable'),
        { method, path, upstream_status: r.status },
      )
    }
    throw _err(r.status, code, message, context)
  }
  try {
    return await r.json()
  } catch {
    throw _err(
      502,
      options?.responseContractErrorCode || 'response_decode_failed',
      options?.responseContractMessage || getT()('auth.projectMutation.responseInvalidJson'),
      { method, path, upstream_status: r.status },
    )
  }
}

// ---- 认证 ----
export async function apiAuthConfig(): Promise<AuthRuntimeConfig> {
  return _request('GET', '/api/auth/config')
}
export async function apiRegister(username: string, password: string): Promise<{ token: string; user: AuthUser }> {
  const r = await _request('POST', '/api/auth/register', { username, password })
  setToken(r.token)
  return r
}
export async function apiLogin(username: string, password: string): Promise<{ token: string; user: AuthUser }> {
  const r = await _request('POST', '/api/auth/login', { username, password })
  setToken(r.token)
  return r
}

async function repairSharedSessionCookie(): Promise<void> {
  // A Set-Cookie from an older login may arrive after the local account has
  // already switched.  Re-assert the current local identity (or clear the
  // cookie after logout).  Repeat only when another switch raced this repair.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const current = getToken()
    try {
      if (current) {
        await _request(
          'POST', '/api/auth/session-cookie', { token: current },
          { tokenSnapshot: current },
        )
      } else {
        await _request('POST', '/api/auth/logout')
      }
    } catch { /* best effort: Bearer/query transport remains authoritative */ }
    if (getToken() === current) return
  }
}

export async function apiPersistSessionCookie(token: string): Promise<{ token: string; user: AuthUser }> {
  const r = await _request('POST', '/api/auth/session-cookie', { token })
  // Response ownership is bound to the token that initiated persistence.
  // Never let a late A response overwrite a newer B login (or resurrect A
  // after logout).  The response may already have changed the HttpOnly cookie,
  // so repair that ambient credential without changing local account state.
  if (getToken() !== token) {
    void repairSharedSessionCookie()
    return r
  }
  setToken(r.token || token)
  return r
}
export async function apiLoginWithLark(): Promise<{ token: string; user: AuthUser }> {
  const t = getT()
  return new Promise((resolve, reject) => {
    let es: EventSource | null = null
    let done = false
    let authWindow: Window | null = null
    const endpoint = new URL(`${BASE}/api/auth/lark/sse`, window.location.origin)
    endpoint.searchParams.set('brand', 'ZE')

    const settle = (fn: () => void) => {
      if (done) return
      done = true
      window.clearTimeout(timer)
      es?.close()
      fn()
    }
    const fail = (code: string, message: string) => {
      settle(() => reject(_err(0, code, message)))
    }
    const readJson = (ev: Event): Record<string, unknown> => {
      try { return JSON.parse((ev as MessageEvent<string>).data) }
      catch { return {} }
    }

    const timer = window.setTimeout(() => {
      fail('auth_timeout', t('auth.feishuTimeout'))
    }, 10 * 60 * 1000)

    try {
      es = new EventSource(endpoint.toString())
    } catch (e) {
      fail('network_error', t('auth.connectFail', { msg: (e as Error).message }))
      return
    }

    es.addEventListener('auth_url', (ev) => {
      const url = readJson(ev).url
      if (typeof url !== 'string' || !url) {
        fail('auth_failed', t('auth.failed'))
        return
      }
      authWindow = window.open(url, 'ze_feishu_auth')
      if (!authWindow) fail('popup_blocked', t('auth.popupBlocked'))
    })
    es.addEventListener('session', (ev) => {
      const payload = readJson(ev)
      const token = payload.token
      const user = payload.user
      if (typeof token !== 'string' || !user || typeof user !== 'object') {
        fail('auth_failed', t('auth.failed'))
        return
      }
      setToken(token)
      authWindow?.close()
      // 收到 session = 服务端已签发会话(token+user 同帧带回),登录此刻即成功。服务端推完
      // session 会立即关流 → EventSource 随即 onerror;必须先同步「认领」(settle 置 done +
      // 关 es + 清 timer),否则关流的 onerror 抢在下面异步之前 reject → 用户看到「失败」,
      // 但 token 已写入 → 刷新后又成已登录态(正是本次 bug 现象)。
      settle(() => resolve({ token, user: user as AuthUser }))
      // cookie 持久化只是跨 ZE 工具 SSO 的加分项,后台尽力而为;失败不影响已成功的登录
      // (Bearer token 已在 localStorage)。
      void apiPersistSessionCookie(token).catch(() => { /* 忽略:已有 Bearer token */ })
    })
    es.addEventListener('auth_error', (ev) => {
      const payload = readJson(ev)
      fail('auth_failed', String(payload.error || t('auth.failed')))
    })
    es.onerror = () => {
      if (done) return
      fail('auth_stream_closed', t('auth.feishuStreamClosed'))
    }
  })
}
export async function apiLogout(): Promise<void> {
  try { await _request('POST', '/api/auth/logout') } catch { /* 已失效也无妨 */ } finally { clearToken() }
}
export async function apiMe(): Promise<AuthUser | null> {
  try {
    const r = await _request('GET', '/api/auth/me')
    return r.user
  } catch {
    return null      // 启动还原:token 失效 → 视为未登录(401 已顺带清 token)
  }
}
export async function apiListUsers(): Promise<string[]> {
  const r = await _request('GET', '/api/auth/users')
  return r.users ?? []
}
export async function apiSearchUsers(
  query = '',
  options?: { limit?: number; channel?: string },
): Promise<ZeUserCard[]> {
  const params = new URLSearchParams()
  if (query.trim()) params.set('query', query.trim())
  params.set('limit', String(options?.limit ?? 50))
  if (options?.channel) params.set('channel', options.channel)
  const r = await _request('GET', `/api/auth/user-directory?${params.toString()}`)
  return r.users ?? []
}
export async function apiLookupUsers(
  ids: string[],
  options?: { channel?: string },
): Promise<{ users: ZeUserCard[]; missing: string[] }> {
  const r = await _request('POST', '/api/auth/user-directory/lookup', {
    ids,
    channel: options?.channel ?? '',
  })
  return { users: r.users ?? [], missing: r.missing ?? [] }
}

// ---- 当前账号绑定(外部 API Key / Codex / Claude profile)----
export async function apiListCredentials(): Promise<{
  providers: CredentialProviderInfo[]
  credentials: CredentialRecord[]
}> {
  const r = await _request('GET', '/api/account/credentials')
  return { providers: r.providers ?? [], credentials: r.credentials ?? [] }
}
export type CursorModelOption = {
  id: string
  display_name: string
  description: string
}
export async function apiListCursorModels(): Promise<CursorModelOption[]> {
  const r = await _request('GET', '/api/account/cursor-models')
  return Array.isArray(r.models) ? r.models : []
}
export async function apiSaveCredential(input: {
  id?: string
  provider: CredentialProvider
  label?: string
  secret?: string
  auth_file?: string
  metadata?: Record<string, unknown>
  capabilities?: string[]
}, idempotencyKey?: string): Promise<CredentialRecord> {
  const r = await _request(
    'POST',
    '/api/account/credentials',
    input,
    { idempotencyKey: idempotencyKey ?? newAuthIdempotencyKey('credential.upsert') },
  )
  return r.credential
}
export async function apiVerifyCredential(id: string): Promise<CredentialRecord> {
  const r = await _request(
    'POST',
    `/api/account/credentials/${encodeURIComponent(id)}/verify`,
    undefined,
    { idempotencyKey: newAuthIdempotencyKey(`credential.verify.${id}`) },
  )
  return r.credential
}
export async function apiSetDefaultCredential(id: string): Promise<CredentialRecord> {
  const r = await _request(
    'POST',
    `/api/account/credentials/${encodeURIComponent(id)}/default`,
    undefined,
    { idempotencyKey: newAuthIdempotencyKey(`credential.default.${id}`) },
  )
  return r.credential
}

// ---- 设备/浏览器登录(傻瓜绑定 Codex / Claude)----
export type CliLoginState = {
  login_id: string
  provider: 'codex' | 'claude'
  status: 'pending' | 'awaiting_code' | 'success' | 'error'
  verify_url: string
  user_code: string
  needs_code_paste: boolean
  error: string
}
export async function apiCliLoginStart(provider: 'codex' | 'claude'): Promise<CliLoginState> {
  return await _request('POST', '/api/account/cli-login/start', { provider })
}
export async function apiCliLoginPoll(loginId: string): Promise<CliLoginState> {
  return await _request('POST', '/api/account/cli-login/poll', { login_id: loginId })
}
export async function apiCliLoginSubmitCode(loginId: string, code: string): Promise<CliLoginState> {
  return await _request('POST', '/api/account/cli-login/submit-code', { login_id: loginId, code })
}
export async function apiCliLoginCancel(loginId: string): Promise<void> {
  await _request('POST', `/api/account/cli-login/cancel?login_id=${encodeURIComponent(loginId)}`)
}
/** R-C17 项目级 git 关联:owner/repo/ref 属项目(服务端 sidecar);PAT 留账号级。 */
export type ProjectGitLink = { owner: string; repo: string; ref: string; updated_at: number }

export async function apiGetProjectGitLink(
  projectId: string,
): Promise<{ git_link: ProjectGitLink | null }> {
  return _request('GET', `/api/projects/${encodeURIComponent(projectId)}/git-link`)
}

export async function apiPutProjectGitLink(
  projectId: string,
  body: { owner: string; repo: string; ref?: string },
): Promise<{ git_link: ProjectGitLink }> {
  return _request(
    'PUT', `/api/projects/${encodeURIComponent(projectId)}/git-link`, body,
    { idempotencyKey: newAuthIdempotencyKey('project.git_link.put') },
  )
}

export async function apiDeleteProjectGitLink(
  projectId: string,
): Promise<{ deleted: boolean }> {
  return _request(
    'DELETE', `/api/projects/${encodeURIComponent(projectId)}/git-link`, undefined,
    { idempotencyKey: newAuthIdempotencyKey('project.git_link.delete') },
  )
}

export async function apiDeleteCredential(id: string): Promise<boolean> {
  const r = await _request(
    'DELETE',
    `/api/account/credentials/${encodeURIComponent(id)}`,
    undefined,
    { idempotencyKey: newAuthIdempotencyKey(`credential.delete.${id}`) },
  )
  return !!r.deleted
}
export async function apiGetAccountModelPolicy(): Promise<AccountModelPolicy> {
  return await _request('GET', '/api/account/model-policy')
}
export async function apiRequestEditPermission(reason = ''): Promise<{
  ok: boolean
  status: 'already_granted' | 'recorded'
  tool_id: string
  current_permission: AuthPermission
  requested_permission?: AuthPermission
}> {
  return await _request('POST', '/api/account/permission-request', {
    permission: 'edit',
    reason,
  })
}
export async function apiAdminListPolicies(): Promise<unknown> {
  return await _request('GET', '/api/admin/policies')
}
export async function apiAdminUpdatePolicy(input: {
  user_id: string
  tool_id?: string
  permission?: AuthPermission
  model_scopes?: Partial<Record<ModelScope, boolean>>
}): Promise<unknown> {
  return await _request('POST', '/api/admin/policies', input)
}

// ---- 项目 ----
const PROJECT_MUTATION_STATUS_VALUES = new Set<ProjectMutationStatus['status']>([
  'committed', 'deleted', 'in_progress', 'repair_required', 'not_committed', 'conflict',
])
const GUEST_SCOPE_RE = /^[0-9a-f]{64}$/

function responseContractFailure(
  code: 'response_contract_invalid' | 'project_mutation_response_invalid' | 'project_mutation_proof_invalid',
  message: string,
  context: Record<string, unknown> = {},
): never {
  // 502 is deliberate: a syntactically successful upstream response whose
  // business identity cannot be verified is not a definitive client failure.
  throw _err(502, code, message, context)
}

function responseRecord(
  value: unknown,
  code: 'response_contract_invalid' | 'project_mutation_response_invalid' | 'project_mutation_proof_invalid',
  context: Record<string, unknown> = {},
): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    responseContractFailure(code, getT()('auth.projectMutation.responseObjectRequired'), context)
  }
  return value as Record<string, unknown>
}

function projectMutationSuccess(
  value: unknown,
  expected: {
    intentId: string
    status: 'committed' | 'deleted'
    kind: ProjectTopologyIntentKind
    projectId?: string
    requireDemoProject?: boolean
    requireOk?: boolean
  },
): ProjectMutationCommit {
  const body = responseRecord(value, 'project_mutation_response_invalid', {
    expected_intent_id: expected.intentId,
    expected_project_id: expected.projectId ?? '',
  })
  const projectId = body.project_id
  const generation = body.generation
  const valid = typeof projectId === 'string' && projectId.length > 0
    && body.intent_id === expected.intentId
    && body.status === expected.status
    && body.kind === expected.kind
    && body.mutation_outcome === 'committed'
    && Number.isSafeInteger(generation) && (generation as number) >= 1
    && (body.project_generation === undefined || body.project_generation === generation)
    && (expected.projectId === undefined || projectId === expected.projectId)
    && (!expected.requireDemoProject || projectId.startsWith('__demo__'))
    && (!expected.requireOk || body.ok === true)
  if (!valid) {
    responseContractFailure(
      'project_mutation_response_invalid',
      getT()('auth.projectMutation.responseMismatch'),
      {
        expected_intent_id: expected.intentId,
        expected_project_id: expected.projectId ?? '',
        expected_status: expected.status,
      },
    )
  }
  return {
    projectId: projectId as string,
    generation: generation as number,
    kind: expected.kind,
  }
}

function projectMutationProof(
  value: unknown,
  expected: {
    intentId: string
    kind: ProjectTopologyIntentKind
    projectId?: string
    requireDemoProject?: boolean
  },
): ProjectMutationStatus {
  const body = responseRecord(value, 'project_mutation_proof_invalid', {
    expected_intent_id: expected.intentId,
    expected_project_id: expected.projectId ?? '',
  })
  const projectId = body.project_id
  const status = body.status
  const generation = body.generation
  const phase = body.phase
  const kind = body.kind
  const proofRevision = body.proof_revision
  const principalScoped = body.principal_scoped
  const intentReserved = body.intent_reserved
  const terminal = status === 'committed' || status === 'deleted'
  const inProgress = status === 'in_progress'
  const valid = typeof projectId === 'string' && projectId.length > 0
    && body.intent_id === expected.intentId
    && typeof status === 'string'
    && PROJECT_MUTATION_STATUS_VALUES.has(status as ProjectMutationStatus['status'])
    && (expected.projectId === undefined || projectId === expected.projectId)
    && (!expected.requireDemoProject || projectId.startsWith('__demo__'))
    && kind === expected.kind
    && proofRevision === 2
    && principalScoped === true
    && typeof intentReserved === 'boolean'
    && body.principal_bound === undefined
    && body.mutation_outcome === undefined
    && (terminal
      ? Number.isSafeInteger(generation) && (generation as number) >= 1
      : generation === undefined)
    && (inProgress
      ? typeof phase === 'string' && phase.length > 0
      : phase === undefined)
    && (!(terminal || inProgress) || intentReserved === true)
  if (!valid) {
    responseContractFailure(
      'project_mutation_proof_invalid',
      getT()('auth.projectMutation.proofMismatch'),
      {
        expected_intent_id: expected.intentId,
        expected_project_id: expected.projectId ?? '',
      },
    )
  }
  return {
    project_id: projectId as string,
    intent_id: expected.intentId,
    status: status as ProjectMutationStatus['status'],
    kind: kind as ProjectTopologyIntentKind,
    ...(generation !== undefined ? { generation: generation as number } : {}),
    ...(phase !== undefined ? { phase: phase as string } : {}),
    proof_revision: 2,
    principal_scoped: true,
    intent_reserved: intentReserved as boolean,
  }
}

function projectSummary(value: unknown, index: number): ProjectSummary {
  const body = responseRecord(value, 'response_contract_invalid', { list: 'projects', index })
  if (typeof body.id !== 'string' || !body.id
      || !Number.isSafeInteger(body.generation) || (body.generation as number) < 1
      || typeof body.name !== 'string'
      || typeof body.owner !== 'string'
      || !Array.isArray(body.collaborators)
      || body.collaborators.some((item) => typeof item !== 'string')
      || typeof body.updated_at !== 'number' || !Number.isFinite(body.updated_at)
      || typeof body.is_owner !== 'boolean') {
    responseContractFailure(
      'response_contract_invalid',
      getT()('auth.projectMutation.projectListItemInvalid'),
      { list: 'projects', index },
    )
  }
  return {
    id: body.id as string,
    generation: body.generation as number,
    name: body.name as string,
    owner: body.owner as string,
    collaborators: [...body.collaborators as string[]],
    updated_at: body.updated_at as number,
    gitLinked: body.git_linked === true,
    is_owner: body.is_owner as boolean,
  }
}

export async function apiListProjects(): Promise<ProjectSummary[]> {
  const r = responseRecord(await _request('GET', '/api/projects', undefined, {
    responseContractErrorCode: 'response_contract_invalid',
    responseContractMessage: getT()('auth.projectMutation.projectListInvalid'),
  }), 'response_contract_invalid', { list: 'projects' })
  if (!Array.isArray(r.projects)) {
    responseContractFailure(
      'response_contract_invalid', getT()('auth.projectMutation.projectListInvalid'),
      { list: 'projects' },
    )
  }
  const projects = (r.projects as unknown[]).map(projectSummary)
  if (new Set(projects.map((project) => project.id)).size !== projects.length) {
    responseContractFailure(
      'response_contract_invalid', getT()('auth.projectMutation.projectListInvalid'),
      { list: 'projects', reason: 'duplicate_id' },
    )
  }
  return projects
}
export async function apiCreateProject(
  input: CreateProjectInput,
  context?: AuthRequestContext,
): Promise<ProjectMutationCommit> {
  const r = await _request('POST', '/api/projects', {
    name: input.name,
    seed: input.seed,
    project_id: input.targetProjectId,
    intent_id: input.intentId,
  }, {
    idempotencyKey: input.idempotencyKey,
    responseContractErrorCode: 'project_mutation_response_invalid',
    responseContractMessage: getT()('auth.projectMutation.createUnknown'),
    tokenSnapshot: context?.authToken,
  })
  return projectMutationSuccess(r, {
    intentId: input.intentId,
    projectId: input.targetProjectId,
    status: 'committed',
    kind: 'create',
  })
}
/** 复制项目(header-v2 §5.10);name 缺省后端给「<原名> 副本」 */
export async function apiDuplicateProject(
  input: DuplicateProjectInput,
  context?: AuthRequestContext,
): Promise<ProjectMutationCommit> {
  const body: Record<string, string | number> = {
    project_id: input.targetProjectId,
    intent_id: input.intentId,
    expected_generation: input.expectedGeneration,
  }
  if (input.name) body.name = input.name
  const r = await _request(
    'POST',
    `/api/projects/${encodeURIComponent(input.sourceProjectId)}/duplicate`,
    body,
    {
      idempotencyKey: input.idempotencyKey,
      responseContractErrorCode: 'project_mutation_response_invalid',
      responseContractMessage: getT()('auth.projectMutation.duplicateUnknown'),
      tokenSnapshot: context?.authToken,
    },
  )
  return projectMutationSuccess(r, {
    intentId: input.intentId,
    projectId: input.targetProjectId,
    status: 'committed',
    kind: 'duplicate',
  })
}
/** 删除项目(header-v2 §5.9);confirmName 服务端硬校验等于项目名(红线双保险) */
export async function apiDeleteProject(
  input: DeleteProjectInput,
  context?: AuthRequestContext,
): Promise<ProjectMutationCommit> {
  const r = await _request('DELETE', `/api/projects/${encodeURIComponent(input.targetProjectId)}`, {
    confirm_name: input.confirmName,
    intent_id: input.intentId,
    expected_generation: input.expectedGeneration,
  }, {
    idempotencyKey: input.idempotencyKey,
    responseContractErrorCode: 'project_mutation_response_invalid',
    responseContractMessage: getT()('auth.projectMutation.deleteUnknown'),
    tokenSnapshot: context?.authToken,
  })
  return projectMutationSuccess(r, {
    intentId: input.intentId,
    projectId: input.targetProjectId,
    status: 'deleted',
    kind: 'delete',
    requireOk: true,
  })
}
/** 权威项目拓扑对账：读取 durable marker/catalog/journal，不触发 mutation replay。 */
export async function apiGetProjectMutationStatus(
  targetProjectId: string,
  intentId: string,
  kind: ProjectTopologyIntentKind,
  context?: AuthRequestContext,
): Promise<ProjectMutationStatus> {
  const r = await _request(
    'GET',
    `/api/projects/${encodeURIComponent(targetProjectId)}/intents/${encodeURIComponent(intentId)}`
      + `?kind=${encodeURIComponent(kind)}`,
    undefined,
    {
      responseContractErrorCode: 'project_mutation_proof_invalid',
      responseContractMessage: getT()('auth.projectMutation.proofInvalid'),
      tokenSnapshot: context?.authToken,
    },
  )
  return projectMutationProof(r, { intentId, kind, projectId: targetProjectId })
}

// ---- demo 模板(一等文件系统能力:多模板 catalog + 隐藏可重置沙盒)----
export type DemoTemplateMeta = {
  id: string
  name: string
  description: string
  category: string
  business_scene: string
  /** Current private sandbox generation for this principal; 0 means absent. */
  sandbox_generation: number
  sandbox_project_id: string
}
export type DemoTemplateCatalog = {
  templates: DemoTemplateMeta[]
  /**
   * Server-issued stable owner scope for the current account/HttpOnly guest
   * session.  The frontend uses it only to partition unresolved intent state.
   */
  guestScope: string
}
function demoTemplate(value: unknown, index: number): DemoTemplateMeta {
  const body = responseRecord(value, 'response_contract_invalid', { list: 'demo_templates', index })
  if (typeof body.id !== 'string' || !body.id
      || typeof body.name !== 'string'
      || typeof body.description !== 'string'
      || typeof body.category !== 'string'
      || typeof body.business_scene !== 'string'
      || !Number.isSafeInteger(body.sandbox_generation)
      || (body.sandbox_generation as number) < 0
      || typeof body.sandbox_project_id !== 'string'
      || !body.sandbox_project_id.startsWith('__demo__')) {
    responseContractFailure(
      'response_contract_invalid',
      getT()('auth.projectMutation.demoTemplateItemInvalid'),
      { list: 'demo_templates', index },
    )
  }
  return {
    id: body.id as string,
    name: body.name as string,
    description: body.description as string,
    category: body.category as string,
    business_scene: body.business_scene as string,
    sandbox_generation: body.sandbox_generation as number,
    sandbox_project_id: body.sandbox_project_id as string,
  }
}

/** 列出示例场景，并取得与 HttpOnly guest cookie 同源的稳定意图 owner scope。 */
export async function apiListDemoTemplates(
  options: { requireGuestScope?: boolean } = {},
): Promise<DemoTemplateCatalog> {
  const r = responseRecord(await _request('GET', '/api/demo-templates', undefined, {
    responseContractErrorCode: 'response_contract_invalid',
    responseContractMessage: getT()('auth.projectMutation.demoListInvalid'),
  }), 'response_contract_invalid', { list: 'demo_templates' })
  if (!Array.isArray(r.templates)) {
    responseContractFailure(
      'response_contract_invalid',
      getT()('auth.projectMutation.demoListInvalid'),
      { list: 'demo_templates' },
    )
  }
  const guestScope = r.guest_scope
  if ((guestScope !== undefined && (
    typeof guestScope !== 'string' || !GUEST_SCOPE_RE.test(guestScope)
  )) || (options.requireGuestScope && (
    typeof guestScope !== 'string' || !GUEST_SCOPE_RE.test(guestScope)
  ))) {
    responseContractFailure(
      'response_contract_invalid',
      getT()('auth.projectMutation.guestScopeMissing'),
      { list: 'demo_templates' },
    )
  }
  const templates = (r.templates as unknown[]).map(demoTemplate)
  if (new Set(templates.map((template) => template.id)).size !== templates.length) {
    responseContractFailure(
      'response_contract_invalid', getT()('auth.projectMutation.demoListInvalid'),
      { list: 'demo_templates', reason: 'duplicate_id' },
    )
  }
  return {
    templates,
    guestScope: typeof guestScope === 'string' ? guestScope : '',
  }
}
/** 打开 / 重置一个示例沙盒(确定性隐藏项目;再次调用 = 删旧建新 = 重置)。返回沙盒 project_id。 */
export async function apiOpenDemoTemplate(
  input: OpenDemoTemplateInput,
  context?: AuthRequestContext,
): Promise<ProjectMutationCommit> {
  const r = await _request('POST', `/api/demo-templates/${encodeURIComponent(input.templateId)}/open`,
    {
      intent_id: input.intentId,
      expected_generation: input.expectedGeneration,
    }, {
      idempotencyKey: input.idempotencyKey,
      responseContractErrorCode: 'project_mutation_response_invalid',
      responseContractMessage: getT()('auth.projectMutation.demoResetUnknown'),
      tokenSnapshot: context?.authToken,
    })
  return projectMutationSuccess(r, {
    intentId: input.intentId,
    projectId: input.sandboxProjectId,
    status: 'committed',
    kind: 'replace',
    requireDemoProject: true,
  })
}
/** Demo 对账由服务端使用登录 actor / HttpOnly guest cookie 推导真实 sandbox id。 */
export async function apiGetDemoMutationStatus(
  templateId: string,
  sandboxProjectId: string,
  intentId: string,
  context?: AuthRequestContext,
): Promise<ProjectMutationStatus> {
  const r = await _request(
    'GET',
    `/api/demo-templates/${encodeURIComponent(templateId)}/intents/${encodeURIComponent(intentId)}`
      + '?kind=replace',
    undefined,
    {
      responseContractErrorCode: 'project_mutation_proof_invalid',
      responseContractMessage: getT()('auth.projectMutation.demoProofInvalid'),
      tokenSnapshot: context?.authToken,
    },
  )
  return projectMutationProof(r, {
    intentId, kind: 'replace', projectId: sandboxProjectId, requireDemoProject: true,
  })
}

// ---- 配置中枢(04-ops ConfigRegistry;skill 级)----
export type ConfigItem = {
  key: string
  kind: 'text' | 'path' | 'int' | 'float' | 'bool' | 'enum' | 'secret' | 'endpoint' | 'model-ref' | 'constant'
  label: string
  desc: string
  scope: 'framework' | 'tool' | 'pref' | 'secret'
  secret: boolean
  readonly: boolean
  choices: string[] | null
  group: string
  value: unknown               // secret 类已脱敏为 '***'
}
export type ConfigSnapshot = { config: ConfigItem[]; version: number }
export async function apiGetConfig(): Promise<ConfigItem[]> {
  return (await apiGetConfigSnapshot()).config
}
/** 带进程内 revision 的完整配置快照，供设置面板做乐观并发控制。 */
export async function apiGetConfigSnapshot(): Promise<ConfigSnapshot> {
  const r = await _request('GET', '/api/config')
  return {
    config: r.config ?? [],
    version: Number.isInteger(r.version) && r.version >= 0 ? r.version : 0,
  }
}
/** 提交一批 override;返回被拒项 + 刷新后的全量配置。 */
export async function apiPutConfig(
  updates: Record<string, unknown>,
  idempotencyKey: string,
  expectedVersion: number,
): Promise<{ rejected: { key: string; reason: string }[]; config: ConfigItem[]; version: number }> {
  const body: Record<string, unknown> = { updates, expected_version: expectedVersion }
  const r = await _request('PUT', '/api/config', body, { idempotencyKey })
  return {
    rejected: r.rejected ?? [],
    config: r.config ?? [],
    version: Number.isInteger(r.version) && r.version >= 0 ? r.version : 0,
  }
}
