/**
 * App — ZE Workspace 装配入口。**框架代码,零业务知识**。
 *
 * 账号门:AuthProvider → 未就绪显载入、未登录显 LoginPage、登录后进 ProjectProvider
 * (key=user.id,切号整体重挂)→ Workspace。Workspace 按 projectId 二态:未打开项目显
 * ProjectPicker,打开后 useDocument(projectId) 订 SSE + 灌 entities → header + 中区 + footer。
 *
 * 中区布局 + 新建默认字段是业务知识(mod 在 02-mod/04-layout.config.ts 声明)。
 * `.ze-app` 上的 data-mod=SKILL_ID 是可选的皮肤作用域钩子(皮肤里可写 html[data-mod="..."] 局部覆盖)。
 *
 * B3 壳体组合点分解:chrome 组装(Header/Footer/copilot)在 AppChrome.tsx,
 * 路由 / mod renderers 注入在 AppRenderers.tsx;本文件保留状态接线与命令拦截。
 * core import 统一走门面 `@ze/core/04-user-facing`(旧深路径仍可用,见门面收录纪律)。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  EditorDispatchContext, usePersistedLayout, loadLayout, useInteractionStore,
  AuthProvider, useAuth, ProjectProvider, useProject, LoginPage,
  PreferencesProvider, usePreferences, setModMessages, useT, useAiRoutes, useAiConsent,
  type EntityTypeSchema, type FormActionResultConsumer, type SlotContextValue, type AuthUser,
} from '@ze/core/04-user-facing'
import { subscribeAssistant } from '@ze/core/04-user-facing/01-layout/copilot'
import { WorkspaceChrome } from './AppChrome'
import { MOD_RENDERERS, WorkspaceMain } from './AppRenderers'
import {
  isCurrentRuntimeNamespace,
  selectTargetExpectedGeneration,
  useDocument,
} from './lib/useDocument'
import {
  uploadAsset, downloadManifestFile, downloadProjectExportFile, renameUnrenderedDocument,
  newIdempotencyKey, fetchDiagnose, type AssetRefDict, type SchemaDoctorReport,
} from './lib/api'
import { SchemaDoctorCard } from './SchemaDoctorCard'
import { ProjectLinkDialog } from './ProjectLinkDialog'
import { MenuItem } from '@ze/core/04-user-facing/01-layout/01-header/menus/MenuItem'
import { projectExportDownloadIntent } from './lib/formActionResults'
import { useMediaTickets } from './lib/useMediaTickets'
import { useAiHighlightBridge } from './lib/useAiActivity'
import { useLocalLink } from './lib/useLocalLink'
import { resolveModHeaderConfig } from './lib/modHeaderConfig'
import * as modConfig from '@mod/04-layout.config'
import { AmbientField } from './AmbientField'
import { messages as modMessages } from '@mod/09-i18n'
const { modLayout, newEntityDefaults } = modConfig
const MOD_SKILL_NAME = modConfig.skillName
const SKILL_ID = modConfig.skillId
const HEADER_CONFIG = resolveModHeaderConfig(modConfig)
const PROJECT_STORAGE_NAMESPACE = `${SKILL_ID}|${
  (import.meta.env.VITE_API_BASE as string | undefined)
  || (typeof document !== 'undefined' ? new URL('.', document.baseURI).href : 'build')
}`
// 背景环境光场开关:可选 mod 声明(02-mod/04-layout.config.ts)。走 namespace 取值 + 可选类型,
// 故 core 同步到「未声明本字段」的 mod 也不报错,缺省即 'static' 关闭。
const AMBIENT_BACKGROUND: 'static' | 'field' =
  (modConfig as { ambientBackground?: 'static' | 'field' }).ambientBackground === 'field' ? 'field' : 'static'
const MOD_TREE = loadLayout(modLayout)

// mod 注入式 custom 面板的稳定读取点已迁 AppRenderers.tsx(MOD_RENDERERS);slotValue 仍在本文件组装。

// 注入 mod 业务字典(mod.* 命名空间);渲染前一次性合并。
setModMessages(modMessages)

export default function App() {
  return (
    <PreferencesProvider>
      <AuthProvider>
        <Root />
      </AuthProvider>
    </PreferencesProvider>
  )
}

// 访客身份:后端保留 id `__demo_guest__`(沙盒 owner / enforce_login 对齐),只读权限。
const GUEST_USER_ID = '__demo_guest__'
const GUEST_SESSION_KEY = `ze.guest-session.${SKILL_ID}`

function readGuestSession(): boolean {
  try {
    return sessionStorage.getItem(GUEST_SESSION_KEY) === '1'
  } catch {
    return false
  }
}

function writeGuestSession(active: boolean): void {
  try {
    if (active) sessionStorage.setItem(GUEST_SESSION_KEY, '1')
    else sessionStorage.removeItem(GUEST_SESSION_KEY)
  } catch {
    // Storage denial only disables reload convenience; the current tab still works.
  }
}

function Root() {
  const t = useT()
  const { user, ready } = useAuth()
  // demo「人人可操作、只是不保存」:未登录也可进示例工作台;持久化/账号动作回登录页。
  const [guest, setGuest] = useState(readGuestSession)
  useEffect(() => {
    if (!user) return
    writeGuestSession(false)
    setGuest(false)
  }, [user])
  const enterGuest = () => {
    writeGuestSession(true)
    setGuest(true)
  }
  const exitGuest = () => {
    writeGuestSession(false)
    setGuest(false)
  }
  if (!ready) {
    return (
      <div className="ze-app" data-mod={SKILL_ID}
           style={{ display: 'grid', placeItems: 'center', height: '100vh', color: 'var(--ze-ink-secondary)' }}>
        {t('common.loading')}
      </div>
    )
  }
  if (!user && !guest) {
    return (
      <div className="ze-app" data-mod={SKILL_ID} data-ze-ambient={AMBIENT_BACKGROUND}>
        {AMBIENT_BACKGROUND === 'field' ? <AmbientField /> : null}
        <LoginPage onTryDemo={enterGuest} />
      </div>
    )
  }
  const effectiveUser: AuthUser = user ?? { id: GUEST_USER_ID, name: t('auth.guestName'), permission: 'read' }
  return (
    <ProjectProvider
      key={`${PROJECT_STORAGE_NAMESPACE}:${effectiveUser.id}`}
      storageNamespace={PROJECT_STORAGE_NAMESPACE}
      storageOwnerId={effectiveUser.id}
    >
      <Workspace user={effectiveUser} guest={!user} onExitGuest={exitGuest} />
    </ProjectProvider>
  )
}

function Workspace({ user, guest = false, onExitGuest }: {
  user: AuthUser; guest?: boolean; onExitGuest?: () => void
}) {
  const t = useT()
  const { logout } = useAuth()
  const { theme, setTheme, locale, setLocale } = usePreferences()
  const {
    projectId, projectRevision, projects, recentIds, refresh, demoTemplates, openDemoTemplate,
    openProject, closeProject, createProject, duplicateProject, deleteProject,
    confirmProjectAuthority,
    refreshError, projectMutationIntents, projectMutationError,
  } = useProject()
  const {
    meta, entitiesByType, oplog, serverStatus, dispatchCmd, syncState, persistenceState,
    pipelineRuns, commandSpecs, pipelineSpecs, statusFor,
    canUndo, canRedo, undo, redo, notFound, errors, dismissError, pushError,
    documentReady, projectGeneration, namespaceKey, accessRevoked,
  } = useDocument(projectId, projectRevision)
  const runtimeNamespace = documentReady && projectId
    ? `${projectId}:${projectRevision}:${projectGeneration}`
    : null
  const runtimeNamespaceRef = useRef<string | null>(runtimeNamespace)
  runtimeNamespaceRef.current = runtimeNamespace
  const consumedActionResultsRef = useRef(new Set<string>())
  useEffect(() => { consumedActionResultsRef.current.clear() }, [runtimeNamespace])

  // 当前项目文档明确不存在(404)→ 回 picker,避免死状态。普通项目本由「不可见即清」守卫兜底,
  // 但 demo 沙盒被列表过滤、不经那道守卫,故这里以文档加载结果作权威信号统一兜住。
  useEffect(() => { if (notFound) closeProject() }, [notFound, closeProject])
  useEffect(() => { if (accessRevoked) closeProject() }, [accessRevoked, closeProject])
  const [layout, setLayout] = usePersistedLayout(SKILL_ID, projectId ?? undefined, MOD_TREE)
  // 信封导入提案卡:选包后先 manifest.preview 出提案,用户在卡片里决定严格/机会式导入(消解「压缩包甩回处理」)
  const [importPreview, setImportPreview] = useState<{
    proposal: IngestProposal
    asset: AssetRefDict
    intentKey: string
  } | null>(null)
  const [diagnosisReport, setDiagnosisReport] = useState<SchemaDoctorReport | null>(null)
  const [linkDialogOpen, setLinkDialogOpen] = useState(false)
  // 落地页「导入项目」:先建项目,待新文档就绪再把文件导进去(见下方 effect)。
  const [pendingImportFile, setPendingImportFile] = useState<File | null>(null)
  const interaction = useInteractionStore()
  // F2 高亮桥:AI 改动回执定稿 → 被改实体集点亮跨面板高亮,展示期结束自动清(纯视觉)
  useAiHighlightBridge(interaction.setHighlights, interaction.clearHighlights)
  // §7.3 变更卡「定位到实体」:点变更卡实体 chip → 把它设为选中(Selection Drives Content:属性面板随之显示它)。
  useEffect(() => subscribeAssistant((frame) => {
    if (frame.type !== 'reveal_entity') return
    interaction.onSelect(frame.entityType, frame.entityId)
    // W1-6:带字段的定位再往下走一层 —— 只选中实体的话,属性面板是显示它了,
    // 但用户仍要在一屏字段里自己找哪个被改了。规划要的是「定位到实体/字段」,
    // 此前只做了前半句。属性面板此刻刚随选中重渲,故下一帧再找那一行。
    const field = frame.field
    if (!field) return
    requestAnimationFrame(() => {
      const row = document.querySelector<HTMLElement>(`[data-ze-field="${CSS.escape(field)}"]`)
      if (!row) return
      row.scrollIntoView({ block: 'center', behavior: 'smooth' })
      // 闪一次:CSS 用 [data-ze-flash] 做一次性高亮;到时摘掉属性,免得留一片长亮的行。
      row.setAttribute('data-ze-flash', 'true')
      window.setTimeout(() => row.removeAttribute('data-ze-flash'), 1400)
    })
  }), [interaction.onSelect])
  const [expanded, setExpanded] = useState(false)
  const assetTicketSources = useMemo(() => ({
    entities: entitiesByType,
    pipelineRuns,
    transientImport: importPreview?.asset,
  }), [entitiesByType, importPreview?.asset, pipelineRuns])
  const resolveAssetUrl = useMediaTickets(projectId, projectGeneration, assetTicketSources)

  // Every same-id generation is a distinct runtime namespace.  Canonical data
  // is reset in useDocument; clear the remaining App-owned ephemeral state too.
  useEffect(() => {
    interaction.reset()
    setImportPreview(null)
  }, [interaction.reset, namespaceKey])

  // 全局 undo/redo 快捷键:⌘Z / Ctrl+Z 撤销,⌘⇧Z / Ctrl+Y 重做。
  // 在输入控件(input/textarea/select/contenteditable)内不拦截 — 让浏览器原生文本撤销生效。
  useEffect(() => {
    if (!projectId) return
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return
      const k = e.key.toLowerCase()
      if (k !== 'z' && k !== 'y') return
      // 已被聚焦编辑器的局部快捷键认领(如画布逐笔 ⌘Z 撤一笔,handler 调 preventDefault)→ 全局
      // 让行,避免一次 ⌘Z 触发两套撤销(局部草稿 + 全局 Operation)。与 document/window 冒泡顺序无关。
      if (e.defaultPrevented) return
      const el = e.target as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable) return
      e.preventDefault()
      if (k === 'y' || (k === 'z' && e.shiftKey)) void redo()
      else void undo()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [projectId, undo, redo])

  // meta.id 是内部 Document id;header 的文件级动作要调用 project REST,必须使用当前 projectId。
  const headerDoc = meta && projectId ? {
    id: projectId, generation: projectGeneration, name: meta.name, schema_version: meta.schema_version,
    collaboration_mode: meta.collaboration_mode, _v: meta.version,
    updated_at: 0, data_dir_path: '',
  } : null

  // demo 沙盒态:从当前 doc config 推导(单一真源 = 文档元数据,非另起前端状态)。
  const isDemoSession = (meta?.config as { project_kind?: string } | undefined)?.project_kind === 'demo_session'

  useEffect(() => {
    if (documentReady && projectId && meta) {
      confirmProjectAuthority(projectId, isDemoSession)
    }
  }, [confirmProjectAuthority, documentReady, isDemoSession, meta, projectId])

  // demo「不保存」语义 = 【会话内持久】:刷新保留示例里的调试(系统提示词 / 参考图 / 已出图)。
  // 【移除】原先「落在 demo 会话就 reset-on-load 重开模板」的副作用——它在用户显式点开示例后会【二次重开=
  // 双开】,后端删旧建新重种实体,打乱前端实时同步(SSE),导致 stage 出图等异步结果回不到界面(实测:停掉它后
  // demo open 2→1、异步结果正常回显);该 reset 对「非持久 demo」本就冗余(刷新本就重载),只剩害处。
  // 需要从头来仍可【再点示例模板】触发 openDemoTemplate(删旧建新)手动重置。

  const schemas = useMemo<Record<string, EntityTypeSchema | undefined>>(() => {
    const out: Record<string, EntityTypeSchema | undefined> = {}
    const ds = meta?.data_schema
    if (ds) for (const k of Object.keys(ds)) out[k] = ds[k] as EntityTypeSchema | undefined
    return out
  }, [meta])

  const consumeActionResult = useCallback<FormActionResultConsumer>(async (spec, result) => {
    // This callback instance freezes the click's project/generation/namespace. A project switch
    // creates a new callback, while an old in-flight result sees the current ref and is discarded.
    const intent = projectExportDownloadIntent(
      spec, result, projectId ?? '', projectGeneration, runtimeNamespace, runtimeNamespaceRef.current,
    )
    if (!intent || !projectId) return
    const consumed = consumedActionResultsRef.current
    if (consumed.has(intent.effectKey)) return
    consumed.add(intent.effectKey)
    try {
      await downloadProjectExportFile(intent.file, projectId, projectGeneration)
    } catch (error) {
      consumed.delete(intent.effectKey)
      if (runtimeNamespaceRef.current === runtimeNamespace) {
        pushError(error as Parameters<typeof pushError>[0])
      }
    }
  }, [projectGeneration, projectId, pushError, runtimeNamespace])

  // AI image 路线态(未绑凭据时供 action 门控置灰;3.56「没绑就先说」上生到编辑器动作)。
  const aiRoutes = useAiRoutes()
  // AI 内容合规同意态(首次生成前须接受条款;部署未配 ze.ai.terms_* 时门不激活,加性零影响)。
  const aiConsent = useAiConsent()

  const slotValue = useMemo<SlotContextValue>(() => ({
    userId: user.id,
    skillId: SKILL_ID,
    projectId: projectId ?? undefined,
    entitiesByType,
    selection: interaction.selection,
    onSelect: interaction.onSelect,
    multiSelection: interaction.multiSelection,
    onSelectMulti: interaction.onSelectMulti,
    lastSelected: interaction.lastSelected,
    // C0:共享时间游标(playhead)= 与 selection 同级的第二条跨编辑器联动轴(纯会话态,不进 Operation)。
    currentTime: interaction.currentTime,
    setCurrentTime: interaction.setCurrentTime,
    // F2:跨面板高亮通道(Rendering Hints 层,纯视觉会话态;A 面板选中 → B 面板高亮一组相关对象)。
    highlights: interaction.highlights,
    setHighlights: interaction.setHighlights,
    clearHighlights: interaction.clearHighlights,
    schemas,
    dispatchCmd,
    onAddFor: (t) => {
      // 框架兜底:mod 未给该类型默认值时,用实体类型 id 作名(语言中立;真实默认在 mod 的 04-layout.config.ts)
      const d = newEntityDefaults[t] ?? { name: t, data: {} }
      dispatchCmd('entity.add', { type: t, name: d.name, data: d.data })
    },
    statusFor,
    pipelineRuns,
    commandSpecs,
    pipelineSpecs,
    assetUrl: resolveAssetUrl,
    // 唯一前端「写资产」接缝:图元(W1a mask 提交 / 导图)拿 ref 后再 dispatch 命令落库。
    uploadAsset: projectId && documentReady
      ? (bytes, mime, ext) => uploadAsset(bytes, mime, ext ?? '', projectId, projectGeneration)
      : undefined,
    // mod 注入式 custom 面板的渲染器表(slot editor:'custom' 经 view_state.rendererKey 取用)。
    renderers: MOD_RENDERERS,
    // AI image 路线态:声明 requires_route='image' 的命令入口据此门控(加性)。
    aiRoutes,
    // AI 内容合规同意态:声明 requires_route 的命令入口据此拦首击弹条款(加性)。
    aiConsent,
  }), [
    entitiesByType, interaction.selection, interaction.multiSelection, interaction.lastSelected,
    interaction.currentTime, interaction.highlights, interaction.onSelect, interaction.onSelectMulti,
    interaction.setCurrentTime, interaction.setHighlights, interaction.clearHighlights,
    schemas, dispatchCmd, statusFor, pipelineRuns, commandSpecs, pipelineSpecs, projectId, user.id,
    documentReady, projectGeneration, resolveAssetUrl, aiRoutes, aiConsent,
  ])

  // M3:UserAvatar 的登出/切换暂经 dispatch 拦截转 AuthContext(M4 把 UserAvatar 正式接 useAuth)。
  // 信封 IO 缝(同款拦截先例):export 取 op.external 文件名触发下载;import 先上传 File 转 args.asset。
  // header-v2:document.* 工作台层命令在此拦截转 ProjectContext/REST(原先是死命令 → unknown_command):
  //   open=切项目(写 LRU) / new=建项目 / close=回 picker / duplicate=复制并打开 /
  //   save_as=带名复制(src_doc_id 底本) / delete=REST 删除(服务端 owner+confirm_name 双校验) /
  //   rename=按 payload.doc_id 路由到目标项目的命令通道(行级重命名,不限当前项目)。
  const headerDispatch = (cmd: string, payload?: Record<string, unknown>) => {
    const expectedGenerationFor = (targetId: string): number | undefined => (
      selectTargetExpectedGeneration({
        targetProjectId: targetId,
        currentProjectId: projectId,
        documentReady,
        renderedGeneration: projectGeneration,
        listedGeneration: Number(payload?.expected_generation) || undefined,
      })
    )
    // 访客模式:账号/持久化动作(新建/复制/另存/重命名/删除/登出/切号)需真账号 → 回登录页「登录以保存」。
    if (guest && (cmd === 'document.new' || cmd === 'document.duplicate'
        || cmd === 'document.save_as' || cmd === 'document.rename'
        || cmd === 'document.delete' || cmd === 'auth.logout' || cmd === 'auth.switch')) {
      onExitGuest?.()
      return
    }
    if (cmd === 'auth.logout' || cmd === 'auth.switch') { void logout(); return }
    if (cmd === 'document.open' && typeof payload?.doc_id === 'string') {
      openProject(payload.doc_id)
      return
    }
    if (cmd === 'demo.open' && typeof payload?.id === 'string') {
      // 示例打开/重置:与 document.open 同型拦截转 ProjectContext(command-first,可被脚本/AI 驱动)
      const expectedGeneration = Number(payload.expected_generation)
      if (!Number.isSafeInteger(expectedGeneration) || expectedGeneration < 0) {
        pushError({ status: 409, code: 'project_generation_required',
          message: t('auth.projectMutation.generationRequired') })
        return
      }
      if (expectedGeneration > 0
          && !globalThis.confirm(t('auth.projects.demoResetConfirm'))) return
      void openDemoTemplate(payload.id, expectedGeneration)
        .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
      return
    }
    if (cmd === 'document.new') {
      void createProject(String(payload?.name ?? '') || t('dialog.new.defaultName'), false)
        .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
      return
    }
    if (cmd === 'document.close') { closeProject(); return }
    if (cmd === 'document.duplicate' && typeof payload?.doc_id === 'string') {
      void duplicateProject(
        payload.doc_id,
        undefined,
        undefined,
        expectedGenerationFor(payload.doc_id),
      )
        .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
      return
    }
    if (cmd === 'document.save_as' && typeof payload?.src_doc_id === 'string') {
      void duplicateProject(payload.src_doc_id, String(payload?.name ?? '') || undefined,
        String(payload?.doc_id ?? '') || undefined, expectedGenerationFor(payload.src_doc_id))
        .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
      return
    }
    if (cmd === 'document.delete' && typeof payload?.id === 'string') {
      void deleteProject(
        payload.id,
        String(payload?.confirm_name ?? ''),
        expectedGenerationFor(payload.id),
      )
        .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
      return
    }
    if (cmd === 'document.rename' && typeof payload?.doc_id === 'string' && payload.doc_id !== projectId) {
      // 非当前行没有已渲染 document version：先读目标 snapshot，再用其中的
      // version + generation 发唯一一次 CAS。GET→POST 间变化就 409，禁止刷新版本自动覆盖。
      void renameUnrenderedDocument(
        payload.doc_id,
        String(payload.name ?? ''),
        newIdempotencyKey(`${payload.doc_id}:document.rename`),
      )
        .then(() => refresh())
        .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
      return
    }
    if (cmd === 'manifest.export' && projectId) {
      const requestNamespace = runtimeNamespace
      const requestGeneration = projectGeneration
      if (!requestNamespace || requestGeneration <= 0) return
      void (async () => {
        const res = await dispatchCmd(cmd, payload ?? {})
        if (!isCurrentRuntimeNamespace(runtimeNamespaceRef.current, requestNamespace)) return
        const ext = (res && 'op' in res
          ? (res.op as { external?: { target_ref?: { file?: string } } } | undefined)?.external
          : undefined)
        const file = ext?.target_ref?.file
        if (file && isCurrentRuntimeNamespace(runtimeNamespaceRef.current, requestNamespace)) {
          await downloadManifestFile(file, projectId, requestGeneration)
            .catch((e) => pushError(e))
        }
      })()
      return
    }
    if (cmd === 'manifest.import' && projectId && payload?.file instanceof File) {
      const f = payload.file
      const requestNamespace = runtimeNamespace
      const requestGeneration = projectGeneration
      if (!requestNamespace || requestGeneration <= 0) return
      void (async () => {
        try {
          const intentKey = newIdempotencyKey(`${projectId}:manifest.import:${f.name}`)
          const bytes = await f.arrayBuffer()
          if (!isCurrentRuntimeNamespace(runtimeNamespaceRef.current, requestNamespace)) return
          const ref = await uploadAsset(bytes, f.type || 'application/zip',
            f.name.endsWith('.json') ? '.json' : '.zip', projectId, requestGeneration)
          if (!isCurrentRuntimeNamespace(runtimeNamespaceRef.current, requestNamespace)) return
          // 先 preview(零 patch 只读)出提案 → 弹卡片让用户选导入方式;不直接落库
          const res = await dispatchCmd('manifest.preview', { asset: ref },
            { idempotencyKey: `${intentKey}:preview` })
          if (!isCurrentRuntimeNamespace(runtimeNamespaceRef.current, requestNamespace)) return
          const tr = (res && 'op' in res
            ? (res.op as { external?: { target_ref?: { proposal?: string } } } | undefined)
              ?.external?.target_ref
            : undefined)
          const proposal = tr?.proposal ? (JSON.parse(tr.proposal) as IngestProposal) : null
          if (proposal) setImportPreview({ proposal, asset: ref, intentKey })
        } catch (e) {
          if (!isCurrentRuntimeNamespace(runtimeNamespaceRef.current, requestNamespace)) return
          pushError(e as Parameters<typeof pushError>[0])   // 上传/预览失败也走统一错误条,不静默
        }
      })()
      return
    }
    dispatchCmd(cmd, payload ?? {})
  }

  // 落地页第一触点入口(ProjectPicker 无项目态):导入项目 / 关联 GitHub。都先新建项目(createProject
  // 自动打开),再接既有的每项目流程——导入等新文档就绪后 dispatch manifest.import;GitHub 直接开
  // 项目关联对话框(ProjectLinkDialog)。这样把「新建文件夹/打开文件」范式配齐在第一屏,不必翻文件切换器菜单。
  useEffect(() => {
    if (documentReady && projectId && pendingImportFile) {
      headerDispatch('manifest.import', { file: pendingImportFile })
      setPendingImportFile(null)
    }
    // headerDispatch 每渲染重建但闭包随 documentReady/projectId 刷新,已在 deps;省略以免每渲染重跑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentReady, projectId, pendingImportFile])
  const onImportProject = async (file: File) => {
    const nm = file.name.replace(/\.[^.]*$/, '') || 'Imported project'
    setPendingImportFile(file)
    try { await createProject(nm, false) } catch (e) { setPendingImportFile(null); throw e }
  }
  const onLinkGithub = async () => {
    await createProject(t('auth.projects.linkGithubDefault'), false)   // createProject 自动 openProject → projectId 就绪
    setLinkDialogOpen(true)
  }

  // 「关联本地文件夹」(W2e):客户端镜像/导出目标,单向 server→本地。hook 挂在常驻
  // Workspace(不随设置菜单开合卸载)保证 F4 自动镜像不中断;UI 注入设置区(F8/F9)。
  const localLink = useLocalLink({
    projectId,
    projectGeneration,
    documentVersion: meta?.version,
    documentReady,
    onImportFile: (file) => headerDispatch('manifest.import', { file }),
  })
  // 每次渲染新建元素(廉价):headerDispatch 未 memo,inline 才能拿到当前闭包,
  // 避免 export/import 用到过期的 projectId/generation。菜单关闭时该 append 不渲染。
  // R-C17 关联统一项目级:两块关联从应用级设置菜单迁入「文件 → 项目关联…」对话框。
  // useLocalLink hook 仍常驻 Workspace(F4 自动镜像不随对话框开合中断),对话框只承载渲染。
  const headerOverrides = {
    file_menu_append: (
      <MenuItem
        label={t('projectLink.openEntry')}
        disabled={!projectId}
        onClick={() => setLinkDialogOpen(true)}
      />
    ),
  }

  return (
    <EditorDispatchContext.Provider value={dispatchCmd}>
      <WorkspaceChrome
        skillId={SKILL_ID}
        ambient={AMBIENT_BACKGROUND}
        header={{
          // 左段 = 工具名(mod 声明的产品名);中段项目/文件名走 doc.name —— 两者不同源,不重复
          config: HEADER_CONFIG,
          overrides: headerOverrides,
          doc: headerDoc ?? null,
          user: { id: user.id, name: user.name, email: user.email, avatar_url: user.avatar_url },
          acl: meta?.acl,
          sync: syncState,
          currentUserId: user.id,
          dispatch: headerDispatch,
          theme,
          onThemeChange: setTheme,
          locale,
          onLocaleChange: setLocale,
          projects,
          recentIds,
          onRefreshProjects: () => { void refresh().catch(() => undefined) },
          demoTemplates: demoTemplates.map((d) => ({
            id: d.id,
            name: d.name,
            sandbox_generation: d.sandbox_generation,
          })),
          isDemoSession,
          // 品牌字标 = 回「我的项目」首页(closeProject → 中区渲 ProjectPicker):第一触点任何页
          // 一步回到新建/打开项目落地页,不必翻文件切换器菜单(项目关联作一等入口,非仅菜单)。
          onHome: closeProject,
        }}
        footer={{
          config: { skill_name: MOD_SKILL_NAME },
          server: serverStatus,
          sync: syncState,
          persistence: persistenceState,
          oplog,
          pipelines: pipelineRuns,
          errors,
          canUndo,
          canRedo,
          onUndo: undo,
          onRedo: redo,
          expanded,
          onExpandedChange: setExpanded,
          dispatch: (cmd, payload) => dispatchCmd(cmd, payload ?? {}),
        }}
        guest={guest}
        onExitGuest={onExitGuest}
        refreshError={refreshError}
        projectMutationError={projectMutationError}
        projectMutationIntents={projectMutationIntents}
        errors={errors}
        onDismissError={dismissError}
        runtimeKey={runtimeNamespace ?? namespaceKey ?? 'no-project'}
        copilotProjectId={documentReady ? projectId ?? undefined : undefined}
        copilotExpectedGeneration={documentReady ? projectGeneration : undefined}
        selection={interaction.selection}
        schemaDiagnosis={meta?.schema_diagnosis ?? null}
        onOpenDiagnosis={projectId ? () => {
          void fetchDiagnose(projectId)
            .then(setDiagnosisReport)
            .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
        } : undefined}
        dialogs={importPreview ? (
          <ImportProposalModal
            proposal={importPreview.proposal}
            onCancel={() => setImportPreview(null)}
            onImport={(mode) => {
              const args = mode
                ? { asset: importPreview.asset, mode }
                : { asset: importPreview.asset }
              void dispatchCmd('manifest.import', args, {
                idempotencyKey: `${importPreview.intentKey}:import:${mode ?? 'strict'}`,
              })
                .catch((e) => pushError(e as Parameters<typeof pushError>[0]))
              setImportPreview(null)
            }}
          />
        ) : diagnosisReport ? (
          <SchemaDoctorCard report={diagnosisReport} onClose={() => setDiagnosisReport(null)} />
        ) : linkDialogOpen && projectId ? (
          <ProjectLinkDialog
            projectId={projectId}
            localLink={localLink}
            localDisabled={!documentReady}
            onExport={() => headerDispatch('manifest.export', {})}
            onImportFile={(file) => headerDispatch('manifest.import', { file })}
            locale={locale === 'en' ? 'en' : 'zh'}
            guest={guest}
            onClose={() => setLinkDialogOpen(false)}
          />
        ) : null}
      >
        <WorkspaceMain
          projectId={projectId}
          documentLoaded={!!meta}
          slotValue={slotValue}
          layout={layout}
          onLayoutChange={setLayout}
          formActionResultConsumer={consumeActionResult}
          manualHtml={HEADER_CONFIG.manual_html}
          onImportProject={onImportProject}
          onLinkGithub={onLinkGithub}
        />
      </WorkspaceChrome>
    </EditorDispatchContext.Provider>
  )
}

/**
 * ImportProposalModal — 信封导入提案卡(broker 协商层的 UI 面;设计 plans/cross-skill-interop-broker-design.md §6 P0⑤)。
 * 展示 manifest.preview 的提案:可收 N 项(→ 目标桶)、丢弃 M 项(带理由)、资产数;
 * 用户选「机会式导入」(收可收子集)或「严格导入」(同类工具全收/失败)。只用已注册的 .ze-dialog/.ze-card/.ze-button 视觉壳。
 */
type IngestProposalItem = { src_bucket: string; src_id: string; into_bucket?: string; reason?: string }
type IngestProposal = {
  pack_schema: string
  level: string
  accept: IngestProposalItem[]
  drop: IngestProposalItem[]
  assets: string[]
  warnings: string[]
}

function ImportProposalModal({ proposal, onImport, onCancel }: {
  proposal: IngestProposal
  onImport: (mode?: 'opportunistic') => void
  onCancel: () => void
}) {
  const t = useT()
  const reasonText: Record<string, string> = {
    undeclared: t('importPreview.reasonUndeclared'),
    required_field_unmet: t('importPreview.reasonRequiredFieldUnmet'),
    foreign_anchor_child: t('importPreview.reasonForeignAnchorChild'),
    anchor_singleton_conflict: t('importPreview.reasonAnchorSingletonConflict'),
    unannotated_source: t('importPreview.reasonUnannotatedSource'),
  }
  return (
    <>
      <div className="ze-dialog__backdrop" onClick={onCancel} />
      <div className="ze-dialog" data-open="true" data-size="lg" role="dialog" aria-modal="true">
        <div className="ze-dialog__header">
          <span className="ze-dialog__title">{t('importPreview.title')}</span>
        </div>
        <div className="ze-dialog__body ze-scroll-fade-y">
          <div className="ze-stack">
            <span className="ze-text" data-role="secondary">
              {proposal.pack_schema} · {t('importPreview.level')} {proposal.level}
            </span>
            <span className="ze-text" data-role="secondary">{t('importPreview.hint')}</span>
            <div className="ze-stack">
              <span className="ze-text">{t('importPreview.accept')} ({proposal.accept.length})</span>
              {proposal.accept.map((a) => (
                <span key={`a:${a.src_bucket}/${a.src_id}`} className="ze-text" data-role="secondary">
                  {a.src_bucket}/{a.src_id} → {a.into_bucket}
                </span>
              ))}
            </div>
            <div className="ze-stack">
              <span className="ze-text">{t('importPreview.drop')} ({proposal.drop.length})</span>
              {proposal.drop.map((d) => (
                <span key={`d:${d.src_bucket}/${d.src_id}`} className="ze-text" data-role="secondary">
                  {d.src_bucket}/{d.src_id} · {reasonText[d.reason ?? ''] ?? d.reason}
                </span>
              ))}
            </div>
            <span className="ze-text" data-role="secondary">
              {t('importPreview.assets')} ({proposal.assets.length})
            </span>
          </div>
        </div>
        <div className="ze-dialog__footer">
          <button className="ze-button" data-size="sm" onClick={onCancel}>
            {t('importPreview.cancel')}
          </button>
          <button className="ze-button" data-size="sm" onClick={() => onImport()}>
            {t('importPreview.strict')}
          </button>
          <button className="ze-button" data-variant="primary" data-size="sm"
                  onClick={() => onImport('opportunistic')}>
            {t('importPreview.opportunistic')}
          </button>
        </div>
      </div>
    </>
  )
}

// ErrorToasts(命令失败即时可见层)已迁 AppChrome.tsx —— chrome 组装组合点的一部分。
