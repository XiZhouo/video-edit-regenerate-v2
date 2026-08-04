/**
 * ProjectContext — 当前打开的 project + 可见项目列表。
 *
 * 在 App 内以 `key={user.id}` 重挂 → 切账号时整体卸载重建(projectId 自然清空、旧
 * useDocument/SSE 随之卸载)。projectId 记 localStorage(`ze.project.<user_id>`),
 * 切回时还原上次打开的项目。持久化的 projectId 若已不在可见列表(被移除协作/删除)→
 * 清回 picker。
 */

import {
  createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode,
} from 'react'
import {
  apiCreateProject, apiDeleteProject, apiDuplicateProject, apiListProjects,
  apiGetDemoMutationStatus, apiGetProjectMutationStatus,
  apiListDemoTemplates, apiOpenDemoTemplate,
  getToken,
  type ProjectSummary, type DemoTemplateMeta, type ProjectMutationCommit,
  type ProjectMutationStatus, type AuthRequestContext,
} from './authApi'
import { useAuth } from './AuthContext'
import {
  inspectProjectMutationIntents, loadProjectMutationIntents, prepareProjectMutationIntent,
  projectMutationFingerprint, projectMutationOwnerPartition, retireProjectMutationIntent,
  runProjectMutationIntent, transitionProjectMutationIntent,
  type ProjectMutationIntent, type ProjectMutationIntentInput,
  type ProjectMutationReconciliation,
} from './projectMutationIntent'
import { getT } from '@ze/i18n'

// demo 沙盒 project id 命名空间(后端 bootstrap._demo_sandbox_id)。沙盒被刻意从 /api/projects
// 过滤,故它不在可见列表里 —— 下方「不可见即清回 picker」的守卫须放行这个前缀,否则一打开
// 示例就被踢回 picker。(数据被清空等极端情形仍可经「关闭」回 picker,有逃生口。)
const DEMO_PROJECT_PREFIX = '__demo__'

type ProjectState = {
  projects: ProjectSummary[]
  projectId: string | null
  /**
   * 当前 project namespace 的前端换代序号。同一个 projectId 被 demo reset
   * 原地替换时也会递增，Document/SSE 消费方据此完整重建旧代运行态。
   */
  projectRevision: number
  loading: boolean
  /** 最近一次列表刷新错误；失败时仍保留最后一次成功快照。 */
  refreshError: Error | null
  /** 尚未获得权威终态的项目拓扑意图（刷新页面后仍从 sessionStorage 恢复）。 */
  projectMutationIntents: ProjectMutationIntent[]
  /** Intent tombstone storage is unavailable/corrupt; topology writes fail closed. */
  projectMutationError: Error | null
  /** 最近打开 LRU(id 序;header-v2 文件切换器排序优先段) */
  recentIds: string[]
  refresh: () => Promise<void>
  openProject: (id: string) => void
  closeProject: () => void
  /** Confirm an unlisted candidate only after /api/document proves its kind/access. */
  confirmProjectAuthority: (id: string, isDemoSession: boolean) => void
  createProject: (name: string, seed: boolean) => Promise<string>
  /** 示例场景模板(一等文件系统能力;登录后一次性加载,会话内不变) */
  demoTemplates: DemoTemplateMeta[]
  /** 打开 / 重置一个示例沙盒(再次调用即重置);返回沙盒 project_id 并打开 */
  openDemoTemplate: (id: string, expectedGeneration: number) => Promise<string>
  /** 复制并打开新副本;返回新 id(header-v2 §5.10;newId 供另存为自定 doc_id) */
  duplicateProject: (id: string, name?: string, newId?: string, expectedGeneration?: number) => Promise<string>
  /** 删除(服务端 owner+confirm_name 双校验);删的是当前项目则回 picker */
  deleteProject: (id: string, confirmName: string, expectedGeneration?: number) => Promise<void>
}

const ProjectContext = createContext<ProjectState | null>(null)
export function projectProviderStorageKey(
  storageNamespace: string,
  channel: 'project' | 'recent',
  ownerId: string,
): string {
  if (!storageNamespace.trim()) throw new Error('project storage namespace is required')
  return `ze.${channel}.${encodeURIComponent(storageNamespace)}.${encodeURIComponent(ownerId)}`
}
const projKey = (storageNamespace: string, uid: string) => (
  projectProviderStorageKey(storageNamespace, 'project', uid)
)
// header-v2 LRU:存储上限取 SPEC recent_max 范围上界(50);展示截断由 fileListModel 按 recent_max 钳
const recentKey = (storageNamespace: string, uid: string) => (
  projectProviderStorageKey(storageNamespace, 'recent', uid)
)
const RECENT_STORE_CAP = 50

export type LatestRequestGate = {
  begin: () => number
  isLatest: (requestId: number) => boolean
}

/**
 * Small request-order gate shared by the provider and its race regression test.
 * Starting a newer refresh permanently makes every older completion stale.
 */
export function createLatestRequestGate(): LatestRequestGate {
  let latestRequestId = 0
  return {
    begin: () => {
      latestRequestId += 1
      return latestRequestId
    },
    isLatest: (requestId) => requestId === latestRequestId,
  }
}

/** Run one request while allowing only the latest invocation to publish state. */
export async function runLatestRequest<T>(options: {
  gate: LatestRequestGate
  request: () => Promise<T>
  onSuccess: (value: T) => void
  onError: (error: unknown) => void
  onSettled: () => void
}): Promise<T> {
  const requestId = options.gate.begin()
  try {
    const value = await options.request()
    if (options.gate.isLatest(requestId)) options.onSuccess(value)
    return value
  } catch (error) {
    if (options.gate.isLatest(requestId)) options.onError(error)
    throw error
  } finally {
    if (options.gate.isLatest(requestId)) options.onSettled()
  }
}

export function nextProjectRevision(current: number): number {
  return current >= Number.MAX_SAFE_INTEGER ? 1 : current + 1
}

export function resolveProjectMutationOwnerId(userId: string, guestScope: string): string {
  return userId || guestScope
}

export class ProjectMutationSessionInactiveError extends Error {
  readonly code = 'project_mutation_session_inactive'

  constructor() {
    super('project mutation session is no longer active')
    this.name = 'ProjectMutationSessionInactiveError'
  }
}

export type ProjectMutationSessionFence = AuthRequestContext & {
  ownerId: string
  storageNamespace: string
  isActive: () => boolean
  assertActive: () => void
}

export function createProjectMutationSessionFence(input: {
  ownerId: string
  storageNamespace: string
  authToken: string
  providerIsActive: () => boolean
  currentOwnerId: () => string
  currentStorageNamespace: () => string
  currentAuthToken: () => string
}): ProjectMutationSessionFence {
  const isActive = () => Boolean(
    input.ownerId
    && input.storageNamespace
    && input.providerIsActive()
    && input.currentOwnerId() === input.ownerId
    && input.currentStorageNamespace() === input.storageNamespace
    && input.currentAuthToken() === input.authToken,
  )
  return {
    ownerId: input.ownerId,
    storageNamespace: input.storageNamespace,
    authToken: input.authToken,
    isActive,
    assertActive: () => {
      if (!isActive()) throw new ProjectMutationSessionInactiveError()
    },
  }
}

/** Guard every network edge and never retire evidence after the owner session dies. */
export async function runProjectMutationIntentInSession<T>(options: {
  session: ProjectMutationSessionFence
  intent: ProjectMutationIntent
  execute: () => Promise<T>
  reconcile: () => Promise<ProjectMutationReconciliation<T>>
  transition: (state: ProjectMutationIntent['state'], errorCode?: string) => void
  retire: () => void
}): Promise<T> {
  let value: T
  try {
    value = await runProjectMutationIntent({
      intent: options.intent,
      execute: () => {
        options.session.assertActive()
        return options.execute()
      },
      reconcile: () => {
        options.session.assertActive()
        return options.reconcile()
      },
      transition: options.transition,
      retire: () => {
        if (options.session.isActive()) options.retire()
      },
    })
  } catch (error) {
    // Do not let an outer A handler interpret the result under account B (for
    // example by refreshing a catalog with B's global token).
    options.session.assertActive()
    throw error
  }
  // A response may resolve after unmount/account switch.  Preserve the
  // original-owner tombstone and stop every outer refresh/navigation effect.
  options.session.assertActive()
  return value
}

function projectGenerationRequired(): Error & { code: string; status: number } {
  return Object.assign(
    new Error(getT()('auth.projectMutation.generationRequired')),
    { code: 'project_generation_required', status: 409 },
  )
}

export function requireExpectedProjectGeneration(value: number | undefined): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) throw projectGenerationRequired()
  return value as number
}

/** Bind one destructive demo reset to the exact catalog snapshot the user acted on. */
export function resolveDemoResetIdentity(
  templateId: string,
  expectedGeneration: number | undefined,
  templates: readonly Pick<
    DemoTemplateMeta, 'id' | 'sandbox_generation' | 'sandbox_project_id'
  >[],
): { sandboxProjectId: string; expectedGeneration: number } {
  const matches = templates.filter((item) => item.id === templateId)
  const template = matches.length === 1 ? matches[0] : undefined
  if (!template
      || !Number.isSafeInteger(expectedGeneration)
      || (expectedGeneration as number) < 0
      || template.sandbox_generation !== expectedGeneration
      || !template.sandbox_project_id.startsWith(DEMO_PROJECT_PREFIX)) {
    throw projectGenerationRequired()
  }
  return {
    sandboxProjectId: template.sandbox_project_id,
    expectedGeneration: expectedGeneration as number,
  }
}

export type PersistedProjectCandidateDecision = 'visible' | 'needs_document_authority' | 'reject'

export function classifyPersistedProjectCandidate(
  candidate: string,
  projects: readonly Pick<ProjectSummary, 'id'>[],
): PersistedProjectCandidateDecision {
  if (projects.some((project) => project.id === candidate)) return 'visible'
  if (candidate.startsWith(DEMO_PROJECT_PREFIX)) return 'needs_document_authority'
  return 'reject'
}

/**
 * Anonymous demo sandboxes are intentionally absent from `/api/projects`, and
 * that account-only endpoint answers 401 for a guest.  A same-tab reload may
 * therefore restore a cached demo id only through the narrower document read.
 * The returned decision never grants authority: `useDocument` must still prove
 * the current HttpOnly guest cookie can read this exact sandbox.
 */
export function shouldRestoreGuestDemoCandidateAfterListFailure(input: {
  candidate: string | null
  authenticatedUserId: string
  storageOwnerId: string
  status: number
  code: unknown
}): boolean {
  return (
    !input.authenticatedUserId
    && Boolean(input.storageOwnerId)
    && Boolean(input.candidate?.startsWith(DEMO_PROJECT_PREFIX))
    // 服务器实际抛 code='auth_required'(server.py _require_login/_require_tool_access);
    // 'login_required' 是历史契约漂移的旧值,兼容保留(C8·大整合反哺:status 之外 code 分支不再是死码)。
    && (input.status === 401 || input.code === 'auth_required' || input.code === 'login_required')
  )
}

/**
 * 账号级列表对**访客**本就答 401 —— 那是正常态,不是错误。
 *
 * 与上面那条的分工:`shouldRestoreGuestDemoCandidateAfterListFailure` 决定「要不要还原缓存的
 * demo 沙盒」,因此必须有 candidate;本条只决定「要不要弹错误横幅」。**首次**点「免登录先体验
 * 示例」的访客没有任何缓存 candidate,于是漏过上面那条、掉进 setRefreshError,顶栏就常驻一条
 * 裸的「login required」——demo 入口是我们自己给的,访客不该为此看到报错。
 */
export function isExpectedGuestListRejection(input: {
  authenticatedUserId: string
  status: number
  code: unknown
}): boolean {
  return (
    !input.authenticatedUserId
    && (input.status === 401 || input.code === 'auth_required' || input.code === 'login_required')
  )
}

export type DemoCommitDisposition = 'navigate' | 'refresh_current' | 'ignore'

export function demoCommitDisposition(input: {
  navigationIntent: number
  requestNavigationIntent: number
  currentProjectId: string | null
  committedProjectId: string
}): DemoCommitDisposition {
  if (input.navigationIntent === input.requestNavigationIntent) return 'navigate'
  if (input.currentProjectId === input.committedProjectId) return 'refresh_current'
  return 'ignore'
}

function readRecent(storageNamespace: string, uid: string): string[] {
  if (!uid) return []
  try {
    const raw = JSON.parse(localStorage.getItem(recentKey(storageNamespace, uid)) ?? '[]')
    return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

export function ProjectProvider({
  children,
  storageNamespace,
  storageOwnerId = '',
}: { children: ReactNode; storageNamespace: string; storageOwnerId?: string }) {
  if (!storageNamespace.trim()) throw new Error('ProjectProvider storageNamespace is required')
  const { user } = useAuth()
  const authUid = user?.id ?? ''
  // Anonymous demo mode is not an authenticated principal, but it still needs
  // a stable browser-local partition to restore the same hidden sandbox after
  // reload. Mutation intent ownership remains bound to the opaque guestScope
  // minted from the HttpOnly cookie; this id is used only for project/LRU UI state.
  const storageUid = authUid || storageOwnerId
  const [guestScope, setGuestScope] = useState('')
  // Logged-in intents partition by account. Anonymous demo intents must use the
  // opaque scope minted from the same HttpOnly guest cookie as the backend
  // sandbox owner; a process-wide "guest" constant would mix different users.
  const mutationOwnerId = resolveProjectMutationOwnerId(authUid, guestScope)
  const mutationStorageOwnerId = mutationOwnerId
    ? projectMutationOwnerPartition(storageNamespace, mutationOwnerId)
    : ''
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [projectId, setProjectId] = useState<string | null>(
    null,
  )
  const [projectRevision, setProjectRevision] = useState(0)
  const [recentIds, setRecentIds] = useState<string[]>(() => readRecent(storageNamespace, storageUid))
  const [loading, setLoading] = useState(true)
  const [hasLoadedProjects, setHasLoadedProjects] = useState(false)
  const [refreshError, setRefreshError] = useState<Error | null>(null)
  const [demoTemplates, setDemoTemplates] = useState<DemoTemplateMeta[]>([])
  const [projectMutationIntents, setProjectMutationIntents] = useState<ProjectMutationIntent[]>(
    () => (mutationStorageOwnerId ? loadProjectMutationIntents(mutationStorageOwnerId) : []),
  )
  const [projectMutationError, setProjectMutationError] = useState<Error | null>(null)
  const persistedProjectCandidate = useRef<string | null>(
    storageUid ? localStorage.getItem(projKey(storageNamespace, storageUid)) : null,
  )
  const [pendingAuthorityProjectId, setPendingAuthorityProjectId] = useState<string | null>(null)
  const [authorizedUnlistedProjectIds, setAuthorizedUnlistedProjectIds] = useState<Set<string>>(
    () => new Set(),
  )
  const mutationInFlight = useRef(new Map<string, Promise<unknown>>())
  const refreshGate = useRef(createLatestRequestGate())
  // Any direct navigation or newer navigation-producing mutation invalidates
  // older async completions.  A slow create/reset must not yank the user away
  // from a project they selected while waiting.
  const navigationIntent = useRef(0)
  const providerActive = useRef(true)
  const providerAuthToken = useRef(getToken())
  const currentMutationOwnerId = useRef(mutationOwnerId)
  const currentStorageNamespace = useRef(storageNamespace)
  const currentProjectId = useRef(projectId)
  currentMutationOwnerId.current = mutationOwnerId
  currentStorageNamespace.current = storageNamespace
  currentProjectId.current = projectId

  useEffect(() => {
    providerActive.current = true
    return () => { providerActive.current = false }
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    await runLatestRequest({
      gate: refreshGate.current,
      request: apiListProjects,
      onSuccess: (next) => {
        setProjects(next)
        setHasLoadedProjects(true)
        setRefreshError(null)
        const candidate = persistedProjectCandidate.current
        if (candidate) {
          persistedProjectCandidate.current = null
          const decision = classifyPersistedProjectCandidate(candidate, next)
          if (decision === 'visible') {
            setProjectId(candidate)
          } else if (decision === 'needs_document_authority') {
            // Demo sandboxes are intentionally absent from /api/projects.  Let
            // useDocument perform the authoritative ACL/kind read, but do not
            // grant the prefix a permanent bypass on its own.
            setPendingAuthorityProjectId(candidate)
            setProjectId(candidate)
          } else if (storageUid) {
            localStorage.removeItem(projKey(storageNamespace, storageUid))
          }
        }
      },
      onError: (error) => {
        const status = Number((error as { status?: unknown } | null)?.status)
        const code = (error as { code?: unknown } | null)?.code
        const candidate = persistedProjectCandidate.current
        if (shouldRestoreGuestDemoCandidateAfterListFailure({
          candidate,
          authenticatedUserId: authUid,
          storageOwnerId,
          status,
          code,
        })) {
          // `/api/projects` is account-only.  Keep the demo id merely as a
          // pending routing candidate; the subsequent `/api/document` ACL/kind
          // read is the sole authority and may still reject a stale cookie/id.
          persistedProjectCandidate.current = null
          setPendingAuthorityProjectId(candidate)
          setProjectId(candidate)
          // 访客 demo 还原是**正常态**(账号级列表本就 401),不是错误——直接返回,
          // 不落入下方 setRefreshError,否则示例沙盒顶栏常驻「login required」横幅(C8)。
          return
        }
        if (isExpectedGuestListRejection({ authenticatedUserId: authUid, status, code })) {
          // 访客没有账号级项目,空列表就是**真实答案**;标记已加载,免得一直转圈。
          setProjects([])
          setHasLoadedProjects(true)
          setRefreshError(null)
          return
        }
        if (status === 403 || code === 'acl_denied') {
          // Tool-level revocation is authoritative, not a transient list
          // failure. Purge cached project names/content routing immediately.
          setProjects([])
          setHasLoadedProjects(true)
          setProjectId(null)
          persistedProjectCandidate.current = null
          setPendingAuthorityProjectId(null)
          setAuthorizedUnlistedProjectIds(new Set())
          if (storageUid) localStorage.removeItem(projKey(storageNamespace, storageUid))
        }
        // Other failures are not an authoritative empty list, so the previous
        // successful snapshot stays visible until a later refresh succeeds.
        setRefreshError(error instanceof Error ? error : new Error(String(error)))
      },
      onSettled: () => setLoading(false),
    })
  }, [authUid, storageNamespace, storageOwnerId, storageUid])

  useEffect(() => { void refresh().catch(() => undefined) }, [refresh])
  const refreshDemoTemplates = useCallback(async () => {
    const catalog = await apiListDemoTemplates({ requireGuestScope: !user?.id })
    setDemoTemplates(catalog.templates)
    if (!user?.id) setGuestScope(catalog.guestScope)
  }, [user?.id])

  // 示例模板 catalog 也携带本 principal 的 sandbox generation；409 后会重拉。
  useEffect(() => {
    void refreshDemoTemplates().catch(() => setDemoTemplates([]))
  }, [refreshDemoTemplates])

  // guestScope arrives with the template catalog after the provider mounts.
  // Restore only that exact owner partition; never read a shared guest bucket.
  useEffect(() => {
    if (!mutationStorageOwnerId) {
      setProjectMutationIntents([])
      setProjectMutationError(null)
      return
    }
    const inspection = inspectProjectMutationIntents(mutationStorageOwnerId)
    setProjectMutationIntents(inspection.intents)
    setProjectMutationError(inspection.error)
  }, [mutationStorageOwnerId])

  // 持久化的 ordinary project 必须经权威列表确认；unlisted demo 只在
  // /api/document 已证明访问权/项目类型后持续放行，前缀本身不构成权限。
  useEffect(() => {
    const unlistedAuthorized = Boolean(projectId && (
      pendingAuthorityProjectId === projectId
      || authorizedUnlistedProjectIds.has(projectId)
    ))
    if (projectId && !unlistedAuthorized && hasLoadedProjects && !loading && !refreshError
        && !projects.some((p) => p.id === projectId)) {
      setProjectId(null)
      if (storageUid) localStorage.removeItem(projKey(storageNamespace, storageUid))
    }
  }, [
    authorizedUnlistedProjectIds, hasLoadedProjects, loading,
    pendingAuthorityProjectId, projectId, projects, refreshError, storageNamespace, storageUid,
  ])

  const confirmProjectAuthority = useCallback((id: string, isDemoSession: boolean) => {
    setPendingAuthorityProjectId((current) => current === id ? null : current)
    setAuthorizedUnlistedProjectIds((current) => {
      const next = new Set(current)
      if (isDemoSession) next.add(id)
      else next.delete(id)
      return next
    })
  }, [])

  // 所有「打开」动作的唯一漏斗:写 projectId 持久化 + LRU 入队(进头/去重/超 cap 踢尾)。
  const openProject = useCallback((id: string) => {
    navigationIntent.current += 1
    if (id.startsWith(DEMO_PROJECT_PREFIX) && !authorizedUnlistedProjectIds.has(id)) {
      setPendingAuthorityProjectId(id)
    }
    setProjectId(id)
    if (storageUid) {
      localStorage.setItem(projKey(storageNamespace, storageUid), id)
      setRecentIds((prev) => {
        const next = [id, ...prev.filter((x) => x !== id)].slice(0, RECENT_STORE_CAP)
        localStorage.setItem(recentKey(storageNamespace, storageUid), JSON.stringify(next))
        return next
      })
    }
  }, [authorizedUnlistedProjectIds, storageNamespace, storageUid])

  const closeProject = useCallback(() => {
    navigationIntent.current += 1
    setProjectId(null)
    setPendingAuthorityProjectId(null)
    if (storageUid) localStorage.removeItem(projKey(storageNamespace, storageUid))
  }, [storageNamespace, storageUid])

  const runProjectMutation = useCallback(function runProjectMutation<T>(
    input: ProjectMutationIntentInput,
    execute: (intent: ProjectMutationIntent, context: AuthRequestContext) => Promise<T>,
    reconcile: (
      intent: ProjectMutationIntent,
      context: AuthRequestContext,
    ) => Promise<ProjectMutationReconciliation<T>>,
  ): Promise<T> {
    if (!mutationOwnerId || !mutationStorageOwnerId) {
      return Promise.reject(new Error('project mutation owner scope is not ready'))
    }
    const session = createProjectMutationSessionFence({
      ownerId: mutationOwnerId,
      storageNamespace,
      authToken: providerAuthToken.current,
      providerIsActive: () => providerActive.current,
      currentOwnerId: () => currentMutationOwnerId.current,
      currentStorageNamespace: () => currentStorageNamespace.current,
      currentAuthToken: getToken,
    })
    let prepared: ReturnType<typeof prepareProjectMutationIntent>
    try {
      session.assertActive()
      prepared = prepareProjectMutationIntent(mutationStorageOwnerId, input)
      setProjectMutationError(null)
    } catch (error) {
      const failure = error instanceof Error ? error : new Error(String(error))
      setProjectMutationError(failure)
      return Promise.reject(failure)
    }
    const { intent } = prepared
    setProjectMutationIntents(prepared.intents)
    const existing = mutationInFlight.current.get(intent.intentId) as Promise<T> | undefined
    if (existing) return existing
    const transition = (state: ProjectMutationIntent['state'], code?: string) => {
      try {
        const next = transitionProjectMutationIntent(
          mutationStorageOwnerId, intent.intentId, state, code,
        )
        if (session.isActive()) {
          setProjectMutationIntents(next)
          setProjectMutationError(null)
        }
      } catch (error) {
        if (session.isActive()) {
          setProjectMutationError(error instanceof Error ? error : new Error(String(error)))
        }
        throw error
      }
    }
    const retire = () => {
      try {
        const next = retireProjectMutationIntent(mutationStorageOwnerId, intent.intentId)
        if (session.isActive()) {
          setProjectMutationIntents(next)
          setProjectMutationError(null)
        }
      } catch (error) {
        if (session.isActive()) {
          setProjectMutationError(error instanceof Error ? error : new Error(String(error)))
        }
        throw error
      }
    }
    const context: AuthRequestContext = { authToken: session.authToken }
    const p = runProjectMutationIntentInSession({
      session,
      intent,
      execute: () => execute(intent, context),
      reconcile: () => reconcile(intent, context),
      transition,
      retire,
    })
    mutationInFlight.current.set(intent.intentId, p)
    // `.finally()` would create an ignored rejected Promise.  Two explicit
    // settlement handlers clear only this flight without changing the result.
    const clear = () => {
      if (mutationInFlight.current.get(intent.intentId) === p) {
        mutationInFlight.current.delete(intent.intentId)
      }
    }
    void p.then(clear, clear)
    return p
  }, [mutationOwnerId, mutationStorageOwnerId, storageNamespace])

  const reconcileProjectStatus = useCallback(async <T,>(
    intent: ProjectMutationIntent,
    expected: 'committed' | 'deleted',
    value: (status: ProjectMutationStatus) => T,
    context: AuthRequestContext,
  ): Promise<ProjectMutationReconciliation<T>> => {
    const topologyKind = intent.kind === 'demo-reset' ? 'replace' : intent.kind
    const status = await apiGetProjectMutationStatus(
      intent.targetId, intent.intentId, topologyKind, context,
    )
    if (status.status === expected) return { status: 'committed', value: value(status) }
    if (status.status === 'not_committed') return { status: 'not_committed' }
    if (status.status === 'in_progress') return { status: 'in_progress' }
    return { status: status.status === 'repair_required' ? 'repair_required' : 'conflict' }
  }, [])

  const createProject = useCallback(async (name: string, seed: boolean) => {
    const navigationToken = ++navigationIntent.current
    const commit = await runProjectMutation<ProjectMutationCommit>({
      kind: 'create',
      fingerprint: projectMutationFingerprint('create', { name, seed }),
    }, async (intent, context) => apiCreateProject({
      name,
      seed,
      targetProjectId: intent.targetId,
      intentId: intent.intentId,
      idempotencyKey: intent.idempotencyKey,
    }, context), async (intent, context) => reconcileProjectStatus(intent, 'committed', (status) => ({
      projectId: status.project_id || intent.targetId,
      generation: status.generation as number,
      kind: 'create',
    }), context))
    void refresh().catch(() => undefined)
    if (navigationIntent.current === navigationToken) openProject(commit.projectId)
    return commit.projectId
  }, [refresh, openProject, reconcileProjectStatus, runProjectMutation])

  // 打开 / 重置示例:目录先给出当前 principal 的确定性沙盒身份与 generation。
  // POST、意图墓碑、只读对账必须绑定同一个 sandbox_project_id；否则目录在点击前
  // 被刷新或服务端回了另一个 namespace 时，前端不能把它误认成这次破坏性重置。
  const openDemoTemplate = useCallback(async (id: string, expectedGeneration: number) => {
    const resetIdentity = resolveDemoResetIdentity(id, expectedGeneration, demoTemplates)
    const {
      sandboxProjectId, expectedGeneration: validatedExpectedGeneration,
    } = resetIdentity
    const navigationToken = ++navigationIntent.current
    let commit: ProjectMutationCommit
    try {
      commit = await runProjectMutation<ProjectMutationCommit>({
      kind: 'demo-reset',
      fingerprint: projectMutationFingerprint('demo-reset', {
        templateId: id,
        sandboxProjectId,
        expectedGeneration: validatedExpectedGeneration,
      }),
      targetId: sandboxProjectId,
    }, async (intent, context) => apiOpenDemoTemplate({
      templateId: id,
      sandboxProjectId,
      expectedGeneration: validatedExpectedGeneration,
      intentId: intent.intentId,
      idempotencyKey: intent.idempotencyKey,
    }, context), async (intent, context) => {
      const status = await apiGetDemoMutationStatus(
        id, sandboxProjectId, intent.intentId, context,
      )
      if (status.status === 'committed') {
        return { status: 'committed', value: {
          projectId: status.project_id,
          generation: status.generation as number,
          kind: 'replace' as const,
        } }
      }
      if (status.status === 'not_committed') return { status: 'not_committed' }
      if (status.status === 'in_progress') return { status: 'in_progress' }
      return { status: status.status === 'repair_required' ? 'repair_required' : 'conflict' }
      })
    } catch (error) {
      const status = Number((error as { status?: unknown } | null)?.status)
      const code = (error as { code?: unknown } | null)?.code
      if (status === 409 || code === 'project_generation_stale') {
        await refreshDemoTemplates().catch(() => undefined)
      }
      throw error
    }
    const disposition = demoCommitDisposition({
      navigationIntent: navigationIntent.current,
      requestNavigationIntent: navigationToken,
      currentProjectId: currentProjectId.current,
      committedProjectId: commit.projectId,
    })
    if (disposition === 'navigate') {
      setDemoTemplates((current) => current.map((template) => template.id === id
        ? { ...template, sandbox_generation: commit.generation }
        : template))
      setAuthorizedUnlistedProjectIds((current) => new Set(current).add(commit.projectId))
      openProject(commit.projectId)
      // Demo reset replaces the namespace in place.  projectId commonly remains
      // identical, so selection identity alone cannot restart useDocument/SSE.
      setProjectRevision(nextProjectRevision)
    } else if (disposition === 'refresh_current') {
      // A→B→A while reset was in flight: do not steal navigation, but the
      // currently visible A is now a new generation and must be rebuilt.
      setAuthorizedUnlistedProjectIds((current) => new Set(current).add(commit.projectId))
      setDemoTemplates((current) => current.map((template) => template.id === id
        ? { ...template, sandbox_generation: commit.generation }
        : template))
      setProjectRevision(nextProjectRevision)
    }
    return commit.projectId
  }, [demoTemplates, openProject, refreshDemoTemplates, runProjectMutation])

  const duplicateProject = useCallback(async (
    id: string,
    name?: string,
    newId?: string,
    expectedGeneration?: number,
  ) => {
    const sourceGeneration = requireExpectedProjectGeneration(expectedGeneration)
    const navigationToken = ++navigationIntent.current
    const commit = await runProjectMutation<ProjectMutationCommit>({
      kind: 'duplicate',
      fingerprint: projectMutationFingerprint('duplicate', {
        sourceProjectId: id,
        name: name ?? null,
        requestedProjectId: newId ?? null,
        expectedGeneration: sourceGeneration,
      }),
      sourceId: id,
      ...(newId ? { targetId: newId } : {}),
    }, async (intent, context) => apiDuplicateProject({
      sourceProjectId: id,
      expectedGeneration: sourceGeneration,
      targetProjectId: intent.targetId,
      name,
      intentId: intent.intentId,
      idempotencyKey: intent.idempotencyKey,
    }, context), async (intent, context) => reconcileProjectStatus(intent, 'committed', (status) => ({
      projectId: status.project_id || intent.targetId,
      generation: status.generation as number,
      kind: 'duplicate',
    }), context))
    void refresh().catch(() => undefined)
    if (navigationIntent.current === navigationToken) openProject(commit.projectId)
    return commit.projectId
  }, [refresh, openProject, reconcileProjectStatus, runProjectMutation])

  const deleteProject = useCallback(async (
    id: string,
    confirmName: string,
    expectedGeneration?: number,
  ) => {
    const targetGeneration = requireExpectedProjectGeneration(expectedGeneration)
    navigationIntent.current += 1
    await runProjectMutation<ProjectMutationCommit>({
      kind: 'delete',
      fingerprint: projectMutationFingerprint('delete', {
        projectId: id, confirmName, expectedGeneration: targetGeneration,
      }),
      targetId: id,
    }, async (intent, context) => apiDeleteProject({
      targetProjectId: id,
      expectedGeneration: targetGeneration,
      confirmName,
      intentId: intent.intentId,
      idempotencyKey: intent.idempotencyKey,
    }, context), async (intent, context) => reconcileProjectStatus(intent, 'deleted', (status) => ({
      projectId: status.project_id || intent.targetId,
      generation: status.generation as number,
      kind: 'delete',
    }), context))
    if (storageUid) {
      setRecentIds((prev) => {
        const next = prev.filter((x) => x !== id)
        localStorage.setItem(recentKey(storageNamespace, storageUid), JSON.stringify(next))
        return next
      })
    }
    // The user may switch A→B while A's delete is in flight.  Close only if A
    // is still current when the response arrives; never let an old closure
    // dismiss the newer selection.
    setProjectId((current) => {
      if (current !== id) return current
      if (storageUid) localStorage.removeItem(projKey(storageNamespace, storageUid))
      return null
    })
    void refresh().catch(() => undefined)
  }, [refresh, reconcileProjectStatus, runProjectMutation, storageNamespace, storageUid])

  return (
    <ProjectContext.Provider value={{
      projects, projectId, projectRevision, loading, refreshError,
      projectMutationIntents, projectMutationError,
      recentIds, demoTemplates,
      refresh, openProject, closeProject, confirmProjectAuthority,
      createProject, openDemoTemplate,
      duplicateProject, deleteProject,
    }}>
      {children}
    </ProjectContext.Provider>
  )
}

export function useProject(): ProjectState {
  const ctx = useContext(ProjectContext)
  if (!ctx) throw new Error('useProject must be used within <ProjectProvider>')
  return ctx
}
