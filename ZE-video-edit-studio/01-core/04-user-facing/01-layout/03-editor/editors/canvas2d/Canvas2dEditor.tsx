/**
 * Canvas2dEditor — `canvas2d` editor class:2D 视口(声明式组合功能区)。
 *
 * 本编辑器 = EditorFrame 外壳 + CanvasToolbarZone(画布工具功能区)+ 画布内容(视口/像素层/spatial)。
 * 三路径(spatial / 空 / 默认)统一走 EditorFrame,不再各自硬编码 `.ze-editor` 外框。
 * **模板默认 = 纯视口**(拖入导入 + 缩放/平移);需要绘制能力的 mod 显式传 tools=[...] 才出工具条。
 * 运行态:pending/running → 视口上盖进度环;error → 错误占位 + 重试。
 */

import { useMemo, useRef, useState, type ReactNode } from 'react'
import { useT, type ViewStateScope } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { useEditorShortcuts, type ShortcutSpec } from '../../lib/useEditorShortcuts'
import { CanvasViewport } from '../../primitives/CanvasViewport'
import { MaskCanvas } from '../../primitives/MaskCanvas'
import { useMaskEditor } from '../../primitives/useMaskEditor'
import { MaskToolbarZone, type MaskRunAction } from '../../zones/MaskToolbarZone'
import { Canvas2dSpatial } from '../../primitives/Canvas2dSpatial'
import { Canvas2dLayerStage } from '../../primitives/Canvas2dLayerStage'
import { ErrorPlaceholder } from '../../lib/Skeleton'
import { CanvasBusyOverlay } from '../../lib/CanvasBusyOverlay'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ToolRowZone } from '../../zones/ToolRowZone'
import { DEFAULT_CONSTRAINTS, type GeometryFieldMap, type GizmoConstraints } from '../../primitives/spatialTypes'
import type { ToolId, StrokeRecord } from '../../primitives/canvasTools'
import {
  readLayerEntities, resolveCanvas2dPreset, resolveCanvas2dSourceKind,
  type Canvas2dFeatureFlags, type Canvas2dLayer, type Canvas2dPreset,
} from '../../primitives/canvas2dModel'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus, StatusLookup } from '../../types'

export type Canvas2dEditorProps = {
  /** 编辑器名(横栏左标题;mod 经 slot.name 声明)。缺省回退实体名/类名。 */
  editorName?: string
  /** 注入功能区(已实例化;装配层据 view_state.promptZone/customZones 构造):
   *  extraZones=content 之前(默认路径操作区尾),afterZones=content 之后(工作区底部)。 */
  extraZones?: ReactNode[]
  afterZones?: ReactNode[]
  afterSize?: 'hug' | 'share'
  afterGrow?: number
  entity: EntityDict | null
  assetUrl?: AssetUrlFn
  status?: EntityRunStatus
  bgField?: string                  // 哪个 data 字段是底图 asset_ref(默认自动找)
  preset?: Canvas2dPreset
  features?: Partial<Canvas2dFeatureFlags>
  tools?: ToolId[]                  // 传了才挂工具栏(默认纯视口)
  /** 共享个人视图态作用域;缩放/平移不进领域 Operation/undo。 */
  viewStateScope?: ViewStateScope
  /** 视口持久化 key(每用户×每项目×每 slot):透传给 Canvas2dSpatial —— 记住缩放/平移 + 打开自适应。 */
  viewportPreferenceKey?: string
  // ---- layered 合成模式(1.27.0 实体化:数据源=layer 实体,活动层=全局 selection)----
  /** layer 实体桶名(适配层据此拉实体/选择;Stage 命令 args 用)。 */
  layerType?: string
  /** layer 实体列表(适配层注入;按 data.order 排序由编辑器做)。 */
  layerEntities?: EntityDict[]
  /** 活动层 = 全局 selection[layerType](适配层注入;编辑器不持有选择态)。 */
  activeLayerId?: string | null
  onSelectLayer?: (id: string) => void
  // ---- 可绘制像素层接缝(W1a;opt-in)----
  pixelLayerField?: string          // 正式字段:哪个 data 字段存/读像素图层 asset_ref(seed-from-asset)
  selectionLayerField?: string      // 正式别名:选择/粗选范围像素层字段
  maskField?: string                // 兼容字段:历史 mask 命名,仍指同一张可绘制像素层
  rangeMaskField?: string           // 兼容字段:single-range-layer preset 的旧像素层字段别名
  onPixelLayerCommit?: (pngBytes: Uint8Array) => void // 一笔结束回吐整张像素层 PNG → mod 存 asset + update
  onMaskCommit?: (pngBytes: Uint8Array) => void       // 兼容回调名
  onStroke?: (s: StrokeRecord) => void
  onDropFile?: (file: File) => void
  brushOpacity?: number
  brushFlow?: number
  brushSpacing?: number
  dispatch?: DispatchFn
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  actionCommand?: string
  /** 运行钮文案=可译 chrome:actionLabelKey(走 t())优先,回退 actionLabel(语言中立直显)。 */
  actionLabelKey?: string
  actionLabel?: string
  /** layered 增层命令(缺省回退内置 entity.add;mod 可声明带业务缺省的命令)。 */
  layerAddCommand?: string
  paintCommand?: string
  exportCommand?: string
  // ---- spatial 模式(声明 geometry + entities 才开;否则纯视口,向后兼容)----
  geometry?: GeometryFieldMap
  /** 类型显示名(spatial 模式 toolbar 标题;数据显示名,mod 经 schema.label 提供;无则回退裸 type)。 */
  typeLabel?: string
  entities?: EntityDict[]
  selectedId?: string | null
  /** F2 跨面板高亮通道(Rendering Hints):高亮集内实体画与选中态可区分、可叠加的描边(画布内绘制)。 */
  highlightedIds?: string[]
  onSelect?: (id: string | null) => void
  constraints?: Partial<GizmoConstraints>
  addCommand?: string
  /** opt-in:画框后先收一段文本(如意图)再 dispatch addCommand(透传 Canvas2dSpatial)。 */
  addPromptLabel?: string
  addPromptField?: string
  addPromptPlaceholder?: string
  updateCommand?: string
  deleteCommand?: string
  statusOf?: (id: string) => ReturnType<StatusLookup>
}

function pickRef(data: Record<string, unknown>, field?: string): AssetRefDict | null {
  const asRef = (v: unknown) => (v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? v as AssetRefDict : null)
  if (field) return asRef(data[field])
  for (const v of Object.values(data)) { const r = asRef(v); if (r) return r }
  return null
}

export function Canvas2dEditor({
  editorName,
  extraZones, afterZones, afterSize, afterGrow,
  entity, assetUrl, status, bgField, preset, features, tools, viewStateScope, viewportPreferenceKey,
  layerType, layerEntities, activeLayerId, onSelectLayer,
  pixelLayerField, selectionLayerField, maskField, rangeMaskField, onPixelLayerCommit, onMaskCommit, onStroke, onDropFile,
  brushOpacity, brushFlow, brushSpacing, dispatch, uploadAsset,
  actionCommand, actionLabelKey, actionLabel, layerAddCommand, paintCommand, exportCommand,
  geometry, typeLabel, entities, selectedId, highlightedIds, onSelect, constraints, addCommand, addPromptLabel, addPromptField, addPromptPlaceholder, updateCommand, deleteCommand, statusOf,
}: Canvas2dEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const resolved = resolveCanvas2dPreset({ preset, tools, features })
  const resolvedTools = resolved.tools
  const effectivePixelLayerField = pixelLayerField ?? selectionLayerField ?? maskField ?? rangeMaskField
  // M3 R1:渲染路径判定收口到单一可测真相(分支顺序不变,行为等价)。
  const sourceKind = resolveCanvas2dSourceKind({
    hasGeometryAndEntities: !!(geometry && entities),
    hasEntity: !!entity,
    featuresLayers: resolved.features.layers,
    hasLayerType: !!layerType,
    hasPixelLayerField: !!effectivePixelLayerField,
  })
  const [active, setActive] = useState<ToolId>(resolvedTools?.[0] ?? 'pan')

  // ---- layered 实体接缝:layer 实体 → 栈(按 order);写出口全走命令通道(逐层 CAS)----
  const stageLayers = useMemo<Canvas2dLayer[]>(
    () => (layerEntities ? readLayerEntities(layerEntities) : []),
    [layerEntities],
  )
  const layerEntityOf = (id: string) => layerEntities?.find((e) => e.id === id)
  const onLayerPatch = layerType ? (layerId: string, patch: Partial<Canvas2dLayer>) => {
    const le = layerEntityOf(layerId)
    if (!le) return
    // Canvas2dLayer 与 layer@1 data 同名同型(camelCase 1:1);name 走信封字段单独传
    const { name: nextName, id: _id, ...dataPatch } = patch as Partial<Canvas2dLayer> & { id?: string }
    fire('entity.update', {
      type: layerType, id: layerId,
      patches: dataPatch,
      ...(typeof nextName === 'string' ? { name: nextName } : {}),
      expected_version: le.version,
    })
  } : undefined
  const onLayerPaint = layerType ? (layerId: string, ref: AssetRefDict, kind: Canvas2dLayer['kind']) => {
    const le = layerEntityOf(layerId)
    if (!le) return
    if (paintCommand) {
      fire(paintCommand, { type: layerType, id: layerId, image: ref, expected_version: le.version })
    } else {
      fire('entity.update', {
        type: layerType, id: layerId,
        patches: kind === 'mask-selection' ? { maskRef: ref } : { assetRef: ref },
        expected_version: le.version,
      })
    }
  } : undefined
  const onLayerAdd = layerType ? (layer: Canvas2dLayer) => {
    const { id, name, ...data } = layer
    // 末位 = max(order)+1(非 length:重排/删除后 order 有空洞时 length 会撞已有值)
    const order = Math.max(-1, ...(layerEntities ?? []).map(
      (e) => (typeof e.data.order === 'number' ? (e.data.order as number) : -1),
    )) + 1
    if (layerAddCommand) {
      fire(layerAddCommand, { type: layerType, id, name, data: { ...data, order } })
    } else {
      fire('entity.add', { type: layerType, id, name, data: { ...data, order } })
    }
  } : undefined
  const onCrop = layerType && entity ? (rect: { x: number; y: number; w: number; h: number }) => {
    // 裁切 = 锚画布尺寸 + 全层平移(逐层 CAS;多 op,undo 为多步——批量 op 是后续加性项)
    fire('entity.update', {
      type: entity.type, id: entity.id,
      patches: { width: rect.w, height: rect.h },
      expected_version: entity.version,
    })
    for (const layer of stageLayers) {
      const le = layerEntityOf(layer.id)
      if (!le) continue
      fire('entity.update', {
        type: layerType, id: layer.id,
        patches: { x: layer.x - rect.x, y: layer.y - rect.y },
        expected_version: le.version,
      })
    }
  } : undefined

  // ---- spatial 模式:声明 geometry + entities 才开(sourceKind 驱动;&& 保留 TS 收窄)----
  if (sourceKind === 'spatial' && geometry && entities) {
    const phaseS = status?.phase
    const bgRef = entities.map((e) => pickRef(e.data, bgField)).find(Boolean) ?? null
    const srcS = bgRef && assetUrl ? assetUrl(bgRef) : undefined
    const cons: GizmoConstraints = { ...DEFAULT_CONSTRAINTS, ...constraints }
    return (
      <EditorFrame
        editorClass="canvas2d"
        header={<HeaderZone title={editorName ?? typeLabel ?? entities[0]?.type ?? entity?.type ?? 'canvas2d'} />}
      >
        <div className="ze-editor__canvas-body" data-run-phase={phaseS}>
          <Canvas2dSpatial
            src={srcS} entities={entities} entityType={entities[0]?.type ?? entity?.type ?? ''}
            selectedId={selectedId ?? null} highlightedIds={highlightedIds} onSelect={onSelect ?? (() => {})}
            geometry={geometry} constraints={cons}
            addCommand={addCommand} updateCommand={updateCommand ?? 'entity.update'} deleteCommand={deleteCommand ?? 'entity.delete'}
            addPromptLabel={addPromptLabel} addPromptField={addPromptField} addPromptPlaceholder={addPromptPlaceholder}
            statusOf={statusOf} viewStateScope={viewStateScope} viewportPreferenceKey={viewportPreferenceKey} fire={fire}
          />
          <CanvasBusyOverlay status={status} />
        </div>
      </EditorFrame>
    )
  }

  // layered 实体模式无锚也成立(stage 尺寸回退层外接+960×640);其余无实体路径照旧空态。
  // 可绘制像素层例外:工具功能区必须常驻(不随底图有无显隐),故落到下方像素层路由(渲 header+
  // MaskToolbarZone+拖拽占位画布;Mask* 为内部历史命名),不进裸空态 —— 否则无图时连工具条都没有。
  if (sourceKind === 'empty') {
    return (
      <EditorFrame editorClass="canvas2d" header={<HeaderZone title={editorName ?? 'canvas2d'} />}>
        {onDropFile ? <CanvasViewport src={undefined} onDropFile={onDropFile} /> : <EditorFrame.Empty>{t('editor.noObject')}</EditorFrame.Empty>}
      </EditorFrame>
    )
  }

  const phase = status?.phase
  const hostData = entity?.data ?? {}
  const ref = pickRef(hostData, bgField)
  const src = ref && assetUrl ? assetUrl(ref) : undefined
  const maskRef = effectivePixelLayerField ? pickRef(hostData, effectivePixelLayerField) : null
  const maskSrc = maskRef && assetUrl ? assetUrl(maskRef) : undefined

  // 可绘制像素层:独立编辑器 = header + 统一工具功能区(MaskToolbarZone)+ 画布(MaskCanvas)。
  // 工具栏从 MaskCanvas 内部外提为 chrome 功能区,常驻不随底图显隐;不再用 ToolRowZone(tools)。
  if (sourceKind === 'pixel-layer') {
    return (
      <Canvas2dMaskEditor
        title={editorName ?? (entity ? (entity.name || entity.id.slice(0, 8)) : (typeLabel ?? layerType ?? 'canvas2d'))}
        src={src} maskSrc={maskSrc}
        onMaskCommit={onPixelLayerCommit ?? onMaskCommit} onDropFile={onDropFile}
        brushOpacity={brushOpacity} brushFlow={brushFlow} brushSpacing={brushSpacing}
        status={status}
        runAction={actionCommand && entity ? {
          command: actionCommand, labelKey: actionLabelKey, label: actionLabel || 'Run',
          args: { type: entity.type, id: entity.id },
          disabled: phase === 'running' || phase === 'pending', variant: 'primary',
        } : undefined}
        dispatch={fire}
      />
    )
  }

  return (
    <EditorFrame
      editorClass="canvas2d"
      header={<HeaderZone title={editorName ?? (entity ? (entity.name || entity.id.slice(0, 8)) : (typeLabel ?? layerType ?? 'canvas2d'))} />}
      zones={[
        ((resolvedTools && resolvedTools.length > 0) || (actionCommand && entity)) ? (
          <ToolRowZone
            key="tools"
            tools={resolvedTools}
            active={active}
            onChange={resolvedTools && resolvedTools.length > 0 ? setActive : undefined}
            runAction={actionCommand && entity ? {
              command: actionCommand,
              labelKey: actionLabelKey,
              label: actionLabel || 'Run',
              args: { type: entity.type, id: entity.id },
              disabled: phase === 'running' || phase === 'pending',
              variant: 'primary',
            } : undefined}
            dispatch={fire}
          />
        ) : null,
        ...(extraZones ?? []),
      ].filter(Boolean) as ReactNode[]}
      afterZones={afterZones} afterSize={afterSize} afterGrow={afterGrow}
    >
      <div className="ze-editor__canvas-body" data-run-phase={phase}>
        {phase === 'error' ? (
          <ErrorPlaceholder
            error={status?.error}
            onRetry={status?.run_id && entity ? () => fire('pipeline.retry_failed', { pipeline_id: status.run_id, item_ids: [entity.id] }) : undefined}
          />
        ) : sourceKind === 'layered' ? (
          <>
            <Canvas2dLayerStage
              entity={entity}
              layers={stageLayers}
              activeLayerId={activeLayerId ?? null}
              onSelectLayer={onSelectLayer}
              onLayerPatch={onLayerPatch}
              onLayerAdd={onLayerAdd}
              onLayerPaint={onLayerPaint}
              onCrop={onCrop}
              assetUrl={assetUrl}
              uploadAsset={uploadAsset}
              dispatch={fire}
              bgField={bgField}
              activeTool={active}
              features={resolved.features}
              exportCommand={exportCommand}
              onDropFile={onDropFile}
            />
            <CanvasBusyOverlay status={status} />
          </>
        ) : (
          <>
            <CanvasViewport src={src} onDropFile={onDropFile} />
            <CanvasBusyOverlay status={status} />
          </>
        )}
      </div>
      {/* onStroke 留给开了 tools 的 mod 接;模板默认不绘制 */}
      {onStroke && active !== 'pan' && active !== 'select' && (
        <div className="ze-editor__canvas-hint">{t('editor.toolSelectedHint', { tool: active })}</div>
      )}
    </EditorFrame>
  )
}

/**
 * Canvas2dMaskEditor — 历史命名的像素层绘制辅助 = header + **统一工具功能区**(MaskToolbarZone)+ 画布。
 * 在通用 2D 逻辑里它只是把笔画累积成一张像素资产;mod 后续可把这张图当范围、alpha 或 mask 消费。
 * 工具栏(画笔/橡皮/笔刷/硬度/撤销/重做/清除)从 MaskCanvas 内部外提为 chrome 的功能区,
 * 用 v2 `.ze-editor__zone` 统一规则、常驻不随底图显隐;状态走 useMaskEditor hook 连通工具栏与画布。
 */
function Canvas2dMaskEditor({
  title, src, maskSrc, onMaskCommit, onDropFile,
  brushOpacity, brushFlow, brushSpacing, status, runAction, dispatch,
}: {
  title: string
  src?: string
  maskSrc?: string
  onMaskCommit?: (pngBytes: Uint8Array) => void
  onDropFile?: (file: File) => void
  brushOpacity?: number
  brushFlow?: number
  brushSpacing?: number
  status?: EntityRunStatus
  runAction?: MaskRunAction
  dispatch?: DispatchFn
}): ReactNode {
  const phase = status?.phase
  const running = phase === 'running' || phase === 'pending'
  const m = useMaskEditor({ src, maskSrc, brushOpacity, brushFlow, brushSpacing, onMaskCommit, disabled: running || !src })
  // 画布内逐笔撤销/重做 = 键盘 ⌘Z/⌘⇧Z(+ Win Ctrl 变体),作用域到聚焦的本编辑器。useEditorShortcuts
  // 挂 document、全局 undo 挂 window;keydown 冒泡 document 先于 window —— 本地栈非空时 stopPropagation
  // 吃掉事件、撤一笔(PS 式);栈空则不拦,放行给全局 ⌘Z 撤整条已提交 Operation。两级 ⌘Z 无歧义衔接;
  // 撤销/重做不出按钮(SPEC §222:undo UI 归 footer 全局)。
  const rootRef = useRef<HTMLDivElement>(null)
  const ctrlRef = useRef(m.controls)
  ctrlRef.current = m.controls
  const shortcuts = useMemo<ShortcutSpec[]>(() => {
    const undoIfLocal = (e: KeyboardEvent) => {
      const c = ctrlRef.current
      if (c.disabled || !c.canUndo) return   // 不拦 → 冒泡到 window 全局 undo
      e.preventDefault(); e.stopPropagation(); c.undo()
    }
    const redoIfLocal = (e: KeyboardEvent) => {
      const c = ctrlRef.current
      if (c.disabled || !c.canRedo) return
      e.preventDefault(); e.stopPropagation(); c.redo()
    }
    return [
      { key: 'z', meta: true, shift: true, handler: redoIfLocal },  // ⌘⇧Z 重做(shift 与 ⌘Z 区分)
      { key: 'z', ctrl: true, shift: true, handler: redoIfLocal },
      { key: 'y', ctrl: true, handler: redoIfLocal },               // Ctrl+Y 重做(Win)
      { key: 'z', meta: true, handler: undoIfLocal },               // ⌘Z 撤销
      { key: 'z', ctrl: true, handler: undoIfLocal },               // Ctrl+Z 撤销(Win)
    ]
  }, [])
  useEditorShortcuts(rootRef, shortcuts)
  return (
    <EditorFrame
      editorClass="canvas2d"
      rootRef={rootRef}
      header={<HeaderZone title={title} />}
      zones={[<MaskToolbarZone key="mask-tools" controls={m.controls} runAction={runAction} dispatch={dispatch} />]}
    >
      {/* 点画布即聚焦本编辑器 root(tabIndex=0)—— 后续 ⌘Z 才落在画布作用域而非直接走全局 */}
      <div
        className="ze-editor__canvas-body" data-run-phase={phase}
        onPointerDownCapture={() => rootRef.current?.focus()}
      >
        {phase === 'error'
          ? <ErrorPlaceholder error={status?.error} />
          : src
            ? <MaskCanvas {...m.canvas} />
            : <CanvasViewport src={undefined} onDropFile={onDropFile} />}
        <CanvasBusyOverlay status={status} />
      </div>
    </EditorFrame>
  )
}
