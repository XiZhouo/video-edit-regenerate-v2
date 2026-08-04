/**
 * Scene3dEditor — `scene3d` editor class:3D 预览(mesh / splat / voxel)。
 *
 * **core 不打包 three.js**:渲染后端由 mod 经 SlotContext.renderers.scene3d 注入(一个
 * 接 {src, entity} 的 React 组件)。core 只出框 + 加载/错误态 + 无 renderer 时的 EmptyState。
 * 这样 core 保持依赖轻、可整树复制;重 3D 引擎留在各 mod。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from 'react'
import {
  useScopedViewState, useT, type ViewStateScope,
} from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { useScrollAffordance } from '../../lib/useScrollAffordance'
import { ProgressRing, ErrorPlaceholder } from '../../lib/Skeleton'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { FunctionZone } from '../../zones/FunctionZone'
import { PreviewZone } from '../../zones/PreviewZone'
import { useLazyModule } from '../../lib/lazyModule'
import { ZeIcon } from '../../../lib/zeIcon'
import {
  SCENE3D_PERSONAL_CAMERA_SLICE, normalizeScene3dCameraSnapshot, scene3dPersonalCameraCodec,
  type Scene3dPersonalCameraState,
} from '../../lib/scene3dViewState'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus } from '../../types'
import type { MultiSelection, SelectMultiFn } from '../../../lib/selectionModel'

/**
 * 渲染器契约。core 不画 3D,但定义外壳能下发的**纯视图指令**(无 dispatch):
 *   resetSignal — 每自增一次 = 一次"复位视角"请求(渲染器把相机拉回 framed home)。
 *   wireframe   — 线框开关(渲染器把材质 wireframe 置位)。
 * 渲染器可只认 {src, entity}；此时 layout 必须保持对应 controls=false。只有真实消费
 * resetSignal/wireframe 的 renderer 才能让外壳展示对应按钮。
 */
export type Scene3dDisplayMode = string | { id: string; label?: string; labelKey?: string }
export type Scene3dCameraMode = 'personal' | 'domain'
export type Scene3dCameraChangeMeta = { settled?: boolean }
export type Scene3dControls = {
  resetView?: boolean
  wireframe?: boolean
}
export type Scene3dRendererProps = {
  src?: string
  entity: EntityDict
  entityType?: string
  entities?: EntityDict[]
  selectedId?: string | null
  entitiesByType?: Record<string, EntityDict[]>
  viewState?: Record<string, unknown>
  assetUrl?: AssetUrlFn
  dispatch?: DispatchFn
  onSelectEntity?: (entityType: string, id: string) => void
  statusOf?: (entityType: string, entityId: string) => EntityRunStatus | undefined
  resetSignal?: number
  wireframe?: boolean
  displayMode?: string
  camera?: unknown
  selection?: unknown
  transformMode?: string
  /**
   * `domain`: cameraField+cameraCommand 的业务相机,回调产生 Operation(旧语义不变)。
   * `personal`:当前用户的观察相机,回调只进 view-state store,绝不 dispatch/undo。
   */
  cameraMode?: Scene3dCameraMode
  /** scope/storage identity;渲染器应在它变化时应用新 camera 或回到自身 transient home。 */
  cameraIdentity?: string
  /** 高频 renderer 可省略 settled 让 core debounce;OrbitControls end 应传 settled:true。 */
  onCameraChange?: (camera: unknown, meta?: Scene3dCameraChangeMeta) => void
  onSelect?: (selection: unknown) => void
  onTransformCommit?: (patch: unknown) => void
  /** 会话多选(Interaction State,非领域态):框选/加减选读写 App 层 useInteractionStore。
   *  与 outline/table 的「绑 slot primary」不同,scene3d 画布内可选任意实体类型(节点≠slot
   *  的 primary 是常态,如 slot 绑 playable、视口选 scene_node),故通道 type-qualified 直通:
   *  multiSelection 整表 + onSelectMulti(entityType, ...)。勿与 selectionField/selectionCommand
   *  (领域态,undo 会回退)混用。 */
  multiSelection?: MultiSelection
  onSelectMulti?: SelectMultiFn
}
export type Scene3dRenderer = ComponentType<Scene3dRendererProps>

export type Scene3dEditorProps = {
  entityType?: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  entity: EntityDict | null
  entities?: EntityDict[]
  selectedId?: string | null
  multiSelection?: MultiSelection
  onSelectMulti?: SelectMultiFn
  entitiesByType?: Record<string, EntityDict[]>
  viewState?: Record<string, unknown>
  assetUrl?: AssetUrlFn
  renderer?: Scene3dRenderer
  /** 懒加载渲染器:mod 不 eager 注入 renderer 时,给一个已注册到懒模块表的 id,
   *  core 首次打开按需拉取(loading/error/retry 由 useLazyModule 托管)。core 仍不 import three。 */
  rendererModuleId?: string
  status?: EntityRunStatus
  meshField?: string
  /** 个人观察相机作用域。仅 resolved cameraMode=personal 时消费此 store。 */
  viewStateScope?: ViewStateScope
  /** 领域相机初值字段。personal 模式下仅作个人观察相机的 transient 首开默认。 */
  cameraField?: string
  /** 显式覆盖相机所有权;缺省 cameraCommand→domain,否则 personal。personal 永不 dispatch cameraCommand。 */
  cameraMode?: Scene3dCameraMode
  selectionField?: string
  displayModeField?: string
  displayModes?: Scene3dDisplayMode[]
  /** 外壳按钮 capability；缺省均为 false，避免渲染器不支持时出现死按钮。 */
  controls?: Scene3dControls
  transformMode?: string
  cameraCommand?: string
  selectionCommand?: string
  displayModeCommand?: string
  transformCommand?: string
  onSelectEntity?: (entityType: string, id: string) => void
  statusOf?: (entityType: string, entityId: string) => EntityRunStatus | undefined
  dispatch?: DispatchFn
}

const EMPTY_PERSONAL_CAMERA: Scene3dPersonalCameraState = { camera: null }
const PERSONAL_CAMERA_DEBOUNCE_MS = 180

function pickRef(data: Record<string, unknown>, field?: string): AssetRefDict | null {
  const asRef = (v: unknown) => (v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? v as AssetRefDict : null)
  if (field) return asRef(data[field])
  for (const v of Object.values(data)) { const r = asRef(v); if (r) return r }
  return null
}
function normalizeDisplayModes(modes: Scene3dDisplayMode[] | undefined, t: (key: string) => string): { id: string; label: string }[] {
  return (modes ?? [])
    .map((mode) => typeof mode === 'string'
      ? { id: mode, label: mode }
      : typeof mode?.id === 'string' ? { id: mode.id, label: mode.labelKey ? t(mode.labelKey) : mode.label ?? mode.id } : null)
    .filter((mode): mode is { id: string; label: string } => !!mode && mode.id.length > 0)
}

export function Scene3dEditor({
  entityType, editorName, entity, entities, selectedId, multiSelection, onSelectMulti, entitiesByType, viewState: rendererViewState,
  assetUrl, renderer: Renderer, rendererModuleId, status, meshField, viewStateScope, dispatch,
  cameraField, cameraMode: configuredCameraMode, selectionField, displayModeField, displayModes, controls, transformMode,
  cameraCommand, selectionCommand, displayModeCommand, transformCommand,
  onSelectEntity, statusOf,
}: Scene3dEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const resolvedCameraMode: Scene3dCameraMode = configuredCameraMode ?? (cameraCommand ? 'domain' : 'personal')
  const actionsRef = useScrollAffordance('x')
  // 懒渲染器:有 eager renderer 时传 null(opt-out);否则按 id 拉取。hook 必须无条件调。
  const lazy = useLazyModule<Scene3dRenderer>(Renderer ? null : (rendererModuleId ?? null))
  const personalCamera = useScopedViewState({
    // Domain ownership must not even read the personal preference authority. Switching ownership
    // therefore changes this hook identity and invalidates any delayed personal callback too.
    scope: resolvedCameraMode === 'personal' ? viewStateScope : undefined,
    slice: SCENE3D_PERSONAL_CAMERA_SLICE,
    codec: scene3dPersonalCameraCodec,
    defaultValue: EMPTY_PERSONAL_CAMERA,
  })
  const personalCameraIdentityRef = useRef(personalCamera.identity)
  personalCameraIdentityRef.current = personalCamera.identity
  const pendingPersonalCamera = useRef<{
    identity: string
    timer: ReturnType<typeof setTimeout>
  } | null>(null)

  const cancelPendingPersonalCamera = useCallback((identity?: string) => {
    const pending = pendingPersonalCamera.current
    if (!pending || (identity !== undefined && pending.identity !== identity)) return
    clearTimeout(pending.timer)
    pendingPersonalCamera.current = null
  }, [])

  // Scope switch/unmount cancels delayed bytes. The shared store additionally makes stale setters
  // inert, so a late callback can neither land in the old UI nor be redirected into the new scope.
  useEffect(() => {
    const identity = personalCamera.identity
    return () => cancelPendingPersonalCamera(identity)
  }, [cancelPendingPersonalCamera, personalCamera.identity])

  const commitPersonalCamera = useCallback((camera: unknown, meta?: Scene3dCameraChangeMeta) => {
    const snapshot = normalizeScene3dCameraSnapshot(camera)
    if (!snapshot) return
    const identity = personalCamera.identity
    const commit = () => {
      if (personalCameraIdentityRef.current !== identity) return
      personalCamera.setValue({ camera: snapshot })
    }
    cancelPendingPersonalCamera(identity)
    if (meta?.settled) {
      commit()
      return
    }
    const timer = setTimeout(() => {
      if (pendingPersonalCamera.current?.identity === identity) pendingPersonalCamera.current = null
      commit()
    }, PERSONAL_CAMERA_DEBOUNCE_MS)
    pendingPersonalCamera.current = { identity, timer }
  }, [cancelPendingPersonalCamera, personalCamera.identity, personalCamera.setValue])

  // 外壳视图态(不 dispatch):复位信号 + 线框开关,经 Scene3dRendererProps 下发给渲染器。
  const [resetSignal, setResetSignal] = useState(0)
  const [wireframe, setWireframe] = useState(false)
  const modes = useMemo(() => normalizeDisplayModes(displayModes, t), [displayModes, t])
  const showResetView = controls?.resetView === true
  const showWireframe = controls?.wireframe === true
  useEffect(() => {
    if (!showWireframe) setWireframe(false)
  }, [showWireframe])
  const entityDisplayMode = entity && displayModeField && typeof entity.data[displayModeField] === 'string'
    ? String(entity.data[displayModeField])
    : ''
  const [displayMode, setDisplayMode] = useState(() => entityDisplayMode || modes[0]?.id || '')

  useEffect(() => {
    setDisplayMode(entityDisplayMode || modes[0]?.id || '')
  }, [entity?.id, entityDisplayMode, modes])

  const commitDisplayMode = useCallback((mode: string) => {
    setDisplayMode(mode)
    if (!entity || !displayModeField) return
    if (displayModeCommand) {
      fire(displayModeCommand, { type: entity.type, id: entity.id, display_mode: mode, field: displayModeField, expected_version: entity.version })
    } else {
      fire('entity.update', { type: entity.type, id: entity.id, patches: { [displayModeField]: mode }, expected_version: entity.version })
    }
  }, [displayModeCommand, displayModeField, entity, fire])

  const commitCamera = useCallback((camera: unknown) => {
    if (!entity || !cameraCommand) return
    fire(cameraCommand, { type: entity.type, id: entity.id, camera, field: cameraField, expected_version: entity.version })
  }, [cameraCommand, cameraField, entity, fire])

  const resetCameraView = useCallback(() => {
    if (resolvedCameraMode === 'personal') {
      cancelPendingPersonalCamera(personalCamera.identity)
      personalCamera.resetValue()
    }
    setResetSignal((n) => n + 1)
  }, [cancelPendingPersonalCamera, personalCamera.identity, personalCamera.resetValue, resolvedCameraMode])

  const commitSelection = useCallback((selection: unknown) => {
    if (!entity || !selectionCommand) return
    fire(selectionCommand, { type: entity.type, id: entity.id, selection, field: selectionField, expected_version: entity.version })
  }, [entity, fire, selectionCommand, selectionField])

  const commitTransform = useCallback((patch: unknown) => {
    if (!entity || !transformCommand) return
    fire(transformCommand, { type: entity.type, id: entity.id, patch, expected_version: entity.version })
  }, [entity, fire, transformCommand])

  if (!entity) {
    return (
      <EditorFrame editorClass="scene3d" header={<HeaderZone title={editorName ?? t('editor.noObject')} />}>
        <EditorFrame.Empty>{t('editor.noObject')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const phase = status?.phase
  // header 身份行:优先 slot 名(editorName),实体名作副标题(「场景舞台 · Sort 3D」),与 form/property 一致。
  const objectName = entity.name || entity.id.slice(0, 8)
  const headerTitle = editorName ? `${editorName} · ${objectName}` : objectName
  const ref = pickRef(entity.data, meshField)
  const src = ref && assetUrl ? assetUrl(ref) : undefined
  const entityCamera = cameraField ? entity.data[cameraField] : undefined
  // 默认所有权规则:cameraCommand 存在→domain,否则 personal;但 cameraMode 可显式覆盖。
  // 尤其显式 personal 即使保留 cameraCommand 声明也绝不 dispatch,避免观察视角进 Operation/undo。
  // 个人 durable/memory snapshot 优先;未调整过时 entity camera 仅作 transient 初值,否则 renderer 自行 home/fit。
  const camera = resolvedCameraMode === 'domain'
    ? entityCamera
    : personalCamera.value.camera ?? entityCamera
  const cameraIdentity = resolvedCameraMode === 'personal'
    ? personalCamera.identity
    : `domain\u0000${entity.type}\u0000${entity.id}\u0000${cameraField ?? ''}`
  const selection = selectionField ? entity.data[selectionField] : undefined
  const view = {
    entityType,
    entities,
    selectedId,
    multiSelection,
    onSelectMulti,
    entitiesByType,
    viewState: rendererViewState,
    assetUrl,
    dispatch: fire,
    onSelectEntity,
    statusOf,
    resetSignal,
    wireframe: showWireframe && wireframe,
    displayMode: displayMode || undefined,
    camera,
    cameraMode: resolvedCameraMode,
    cameraIdentity,
    selection,
    transformMode,
    onCameraChange: resolvedCameraMode === 'personal'
      ? commitPersonalCamera
      : cameraCommand ? commitCamera : undefined,
    onSelect: selectionCommand ? commitSelection : undefined,
    onTransformCommit: transformCommand ? commitTransform : undefined,
  }

  // entity 运行错误由 PreviewZone 统一渲染;此处只算非 error 的预览内容。
  let body: ReactNode
  let hasRenderer = false
  if (Renderer) {
    hasRenderer = true
    body = <Renderer src={src} entity={entity} {...view} />
  } else if (rendererModuleId) {
    // 懒加载路径:core 不打包 three,首次打开按需拉取渲染器
    if (lazy.status === 'ready') {
      const LazyRenderer = lazy.value
      hasRenderer = true
      body = <LazyRenderer src={src} entity={entity} {...view} />
    } else if (lazy.status === 'error') {
      body = <ErrorPlaceholder error={{ code: 'renderer_load', message: lazy.error.message }} onRetry={lazy.retry} />
    } else {
      body = <div className="ze-editor__canvas-overlay"><ProgressRing size={48} label={t('editor.loadingRenderer')} /></div>
    }
  } else {
    body = (
      <div className="ze-empty">
        <span className="ze-empty__icon" aria-hidden="true"><ZeIcon name="cube" size="lg" /></span>
        <span className="ze-empty__title">{t('editor.noRenderer')}</span>
        <span className="ze-empty__body">{t('editor.noRendererBody')}</span>
      </div>
    )
  }

  // 操作区:复位/线框(局部视图态触发,非 dispatch)。仅渲染器挂载时显示。
  const viewZone = hasRenderer && (modes.length > 1 || showResetView || showWireframe) ? (
    <FunctionZone key="view" id="toolbar" role="toolbar" kind="tool" layout="hug-scroll">
      <div ref={actionsRef} className="ze-editor__zone-actions ze-scroll-fade-x" role="group">
        {modes.length > 1 ? (
          <span className="ze-select" data-size="sm">
            <select
              className="ze-select__control"
              aria-label={t('editor.preview')}
              value={displayMode}
              onChange={(e) => commitDisplayMode(e.currentTarget.value)}
            >
              {modes.map((mode) => <option key={mode.id} value={mode.id}>{mode.label}</option>)}
            </select>
          </span>
        ) : null}
        {showResetView ? (
          <button type="button" className="ze-button" data-size="sm" data-variant="ghost"
            onClick={resetCameraView}>{t('editor.resetView')}</button>
        ) : null}
        {showWireframe ? (
          <button type="button" className="ze-button" data-size="sm" data-variant={wireframe ? 'primary' : 'ghost'}
            onClick={() => setWireframe((v) => !v)}>{t('editor.wireframe')}</button>
        ) : null}
      </div>
    </FunctionZone>
  ) : undefined

  return (
    <EditorFrame
      editorClass="scene3d"
      header={<HeaderZone title={headerTitle} />}
      zones={viewZone ? [viewZone] : undefined}
    >
      {/* 仅渲染器挂载时叠"处理中"覆盖层;懒加载/未配置时 body 已自带 spinner/空态,避免双层覆盖 */}
      <PreviewZone
        phase={phase}
        error={status?.error}
        onRetry={status?.run_id ? () => fire('pipeline.retry_failed', { pipeline_id: status.run_id, item_ids: [entity.id] }) : undefined}
        busyStatus={status}
        showBusy={hasRenderer}
      >
        {body}
      </PreviewZone>
    </EditorFrame>
  )
}
