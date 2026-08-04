/**
 * slotAdapters — 把 SlotContext 接到各 editor class 的 props(开/闭原则)。
 *
 * 唯一一处"editor_class → 怎么从 SlotContext 取数 + 注入 assetUrl/status/renderer"的映射。
 * 新增 editor class = 在这里加一条 + 在 EDITOR_CLASSES 注册,LayoutRenderer 不用改。
 *
 * 单实体类(form/canvas2d/scene3d/asset/media)取当前选中 entity;多实体类(table/gallery/task)
 * 取整列表。运行态经 statusFor 柯里化注入(占位/进度/错误)。
 */

import { type ReactNode } from 'react'
import { getT } from '@ze/i18n'
import type { ViewStateScope } from '@ze/core/04-user-facing/05-preferences'
import {
  AssetEditor, Canvas2dEditor, ChartEditor, CodeEditor, CompareEditor, FormEditor, GalleryEditor, GraphEditor,
  DialogEditor, DocumentEditor, HtmlEditor, MediaEditor, OutlineEditor, PipelineEditor, ReviewEditor, Scene3dEditor, TableEditor, TaskEditor, TextEditor, TimelineEditor, CurveEditor, PromptZone,
  asCardSize, parseFormActions, parseInspectorSections, parseGalleryActions, parseGridFilters, parseCardSorts, parseRequiredCardFilters,
} from '../03-editor'
import { resolveInspectorTarget } from './inspectorTarget'
import type { ChartKind, GraphFields, TimelineFields, ToolbarAction } from '../03-editor'
import type { GeometryFieldMap, GizmoConstraints } from '../03-editor'
import type {
  EntityDict, EntityRefOptions, EntityTypeSchema, FormActionResultConsumer,
} from '../03-editor'
import type { SlotContextValue } from './SlotContext'
import type { EditorSlotNode } from '../types'

/** entity.data 里某值是 asset_ref dict(含 sha256)则返回它,否则 null。 */
function asAssetRef(v: unknown): { sha256: string } | null {
  return v && typeof v === 'object' && typeof (v as { sha256?: unknown }).sha256 === 'string'
    ? (v as { sha256: string }) : null
}

/** 文件名 → 扩展名(含点,小写;无则空串)。给 uploadAsset 的 ext 用。 */
function _extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

/** 从 view_state 解析几何字段映射(object 形 geometryField | flat 形 x/y/w/h 字段名);都无 → undefined(纯视口)。 */
function parseGeometryFieldMap(view: Record<string, unknown>): GeometryFieldMap | undefined {
  if (typeof view.geometryField === 'string') return { kind: 'object', field: view.geometryField }
  const { xField, yField, wField, hField, rotationField } = view
  if (typeof xField === 'string' && typeof yField === 'string' && typeof wField === 'string' && typeof hField === 'string') {
    return { kind: 'flat', x: xField, y: yField, w: wField, h: hField, ...(typeof rotationField === 'string' ? { rotation: rotationField } : {}) }
  }
  return undefined
}

/** 从 view_state 解析 timeline 的字段映射(track/start/duration 必备,label/color 可选);缺默认裸名。 */
function parseTimelineFields(view: Record<string, unknown>): TimelineFields {
  const s = (k: string, dflt: string) => (typeof view[k] === 'string' ? (view[k] as string) : dflt)
  const opt = (k: string) => (typeof view[k] === 'string' ? (view[k] as string) : undefined)
  return {
    track: s('trackField', 'track'),
    start: s('startField', 'start'),
    duration: s('durationField', 'duration'),
    label: opt('labelField'),
    color: opt('colorField'),
  }
}

/** 从 view_state 解析 graph 的字段映射(x/y 必备,label/deps 可选);缺默认裸名。 */
function parseGraphFields(view: Record<string, unknown>): GraphFields {
  const s = (k: string, dflt: string) => (typeof view[k] === 'string' ? (view[k] as string) : dflt)
  const opt = (k: string) => (typeof view[k] === 'string' ? (view[k] as string) : undefined)
  return { x: s('xField', 'x'), y: s('yField', 'y'), label: opt('labelField'), deps: opt('depsField') }
}

function parseConstraints(view: Record<string, unknown>): Partial<GizmoConstraints> {
  const out: Partial<GizmoConstraints> = {
    rotationEnabled: typeof view.rotationField === 'string' || view.rotationEnabled === true,
  }
  for (const k of ['minW', 'minH', 'maxW', 'maxH', 'aspect'] as const) {
    if (typeof view[k] === 'number') out[k] = view[k] as number
  }
  return out
}

type GalleryLayoutMode = 'grid' | 'card-list' | 'compact-list'

function asGalleryLayoutMode(v: unknown): GalleryLayoutMode | undefined {
  return v === 'grid' || v === 'card-list' || v === 'compact-list' ? v : undefined
}

function parseToolbarActions(v: unknown): ToolbarAction[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out = v.filter((x): x is ToolbarAction => {
    if (!x || typeof x !== 'object') return false
    const a = x as ToolbarAction
    if (typeof a.id !== 'string' || typeof a.labelKey !== 'string') return false
    if (a.kind === 'command') return typeof a.command === 'string'
    if (a.kind === 'toggle' || a.kind === 'search') return typeof a.viewKey === 'string'
    if (a.kind === 'cycle') return typeof a.viewKey === 'string' && Array.isArray(a.options) && a.options.every((o) => typeof o === 'string')
    return false
  })
  return out.length ? out : undefined
}

function asRecord(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : undefined
}

/** 从 active schema 的 entity_ref 字段(带绑定类型 `to`)派生候选表:每个被引用类型 → 其非删实体
 *  的 {id, label}(name 回退 id)。给 FieldRow 的 entity_ref 选择器用(同 variantChildren 惯例:
 *  装配层从 entitiesByType 预派生,编辑器不直读 store)。无 entity_ref 字段或都无 `to` → undefined
 *  (FieldRow 降级为写 {"$ref"} 的文本输入)。 */
function buildEntityRefOptions(
  schema: EntityTypeSchema | undefined,
  entitiesByType: Record<string, EntityDict[]>,
): EntityRefOptions | undefined {
  const props = schema?.properties
  if (!props) return undefined
  let out: EntityRefOptions | undefined
  for (const fs of Object.values(props)) {
    const f = fs as { type?: string; to?: string }
    if (f.type !== 'entity_ref' || typeof f.to !== 'string') continue
    if (out && f.to in out) continue   // 该被引用类型已建桶
    const bucket = entitiesByType[f.to] ?? []
    ;(out ??= {})[f.to] = bucket.filter((e) => !e.deleted).map((e) => ({ id: e.id, label: e.name || e.id }))
  }
  return out
}

function slotViewStateScope(ctx: SlotContextValue, node: EditorSlotNode): ViewStateScope | undefined {
  if (!ctx.userId || !ctx.skillId || !ctx.projectId) return undefined
  return {
    userId: ctx.userId,
    skillId: ctx.skillId,
    projectId: ctx.projectId,
    slotId: node.id,
  }
}

export function renderSlot(
  node: EditorSlotNode,
  ctx: SlotContextValue,
  consumeActionResult?: FormActionResultConsumer,
): ReactNode {
  const t = getT()
  const ec = node.editor_class
  const primary = node.entity_bindings[0]
  if (!primary) return <div className="ze-slot__empty">{t('slot.unbound')}</div>
  const editorName = typeof node.nameKey === 'string' ? t(node.nameKey) : node.name

  const list = ctx.entitiesByType[primary] ?? []
  const selId = ctx.selection[primary] ?? null
  // 选中驱动内容:优先显式选中;未选且该类型只有一个实体 → 回退到它(单物体编辑器即应显示那一个,
  // 不需先手动点选 —— demo 打开 / 导入后立即可见;>1 实体仍要求显式选择以消歧义)。selId 不变,
  // 只决定"显示哪个",不伪造选择态。
  const sel = list.find((e) => e.id === selId) ?? (list.length === 1 ? list[0] : null)
  const statusOf = ctx.statusFor ? (id: string) => ctx.statusFor!(primary, id) : undefined
  const view = (node.view_state ?? {}) as Record<string, unknown>
  const viewStateScope = slotViewStateScope(ctx, node)

  // 功能区注入(编辑器 = 工作区里有序功能区的容器,落位由声明决定、不靠专属槽):每条声明 region —
  // 缺省/'before' = content 之前(操作区,旧行为);'after'/'after-content'/'footer' = content 之后(工作区底部)。
  // ① 内置对话功能区 PromptZone:mod 声明 view_state.promptZone={command, textArg?, placeholderKey?,
  //    region?, assetArg?, accept?}。设 assetArg 即开「传文件」(上传 ref 进命令 args[assetArg],照抄 ChatGPT)。
  // ② 通用 mod custom zone:view_state.customZones=[{rendererKey, region?, id?}],渲染器经 ctx.renderers
  //    (同 custom 编辑器一张表)**原样渲染**——同 custom 编辑器逃生舱,不强加外壳:mod 自定结构(纵向面板/
  //    行皆可),要标准 zone 几何可自行用 core 导出的 FunctionZone。透传 CustomPaneProps 同款受控 ctx
  //    (只读快照 + dispatch + uploadAsset);任意 skill 在 mod 加特殊功能区,core 零业务、写只走命令。
  const beforeZones: ReactNode[] = []
  const afterZones: ReactNode[] = []
  // afterZones 的纵向尺寸(透给 EditorFrame 的 .ze-editor__after):由 customZone 的 size/grow 声明,
  // 缺省 hug(内容高贴底,= 旧行为);share=与 content 按 grow 比例瓜分剩余高。
  let afterSize: 'hug' | 'share' | undefined
  let afterGrow: number | undefined
  const isAfterRegion = (r: unknown) => r === 'after' || r === 'after-content' || r === 'footer'
  const pushZone = (n: ReactNode, region: unknown) => { (isAfterRegion(region) ? afterZones : beforeZones).push(n) }

  const promptCfg = asRecord(view.promptZone)
  if (promptCfg && typeof promptCfg.command === 'string') {
    pushZone(
      <PromptZone
        key="prompt"
        command={promptCfg.command}
        textArg={typeof promptCfg.textArg === 'string' ? promptCfg.textArg : undefined}
        placeholderKey={typeof promptCfg.placeholderKey === 'string' ? promptCfg.placeholderKey : undefined}
        assetArg={typeof promptCfg.assetArg === 'string' ? promptCfg.assetArg : undefined}
        accept={typeof promptCfg.accept === 'string' ? promptCfg.accept : undefined}
        type={primary}
        target={selId ?? undefined}
        uploadAsset={ctx.uploadAsset}
        assetUrl={ctx.assetUrl}
        dispatch={ctx.dispatchCmd}
      />,
      promptCfg.region,
    )
  }

  const customZoneCfgs = Array.isArray(view.customZones) ? view.customZones : []
  customZoneCfgs.forEach((raw, i) => {
    const cz = asRecord(raw)
    if (!cz || typeof cz.rendererKey !== 'string') return
    const Comp = ctx.renderers?.[cz.rendererKey]
    if (!Comp) return   // 渲染器未注册 → 该功能区静默缺席(mod 检查 02-mod/05-renderers 注册)
    const czId = typeof cz.id === 'string' ? cz.id : `custom-${i}`
    if (isAfterRegion(cz.region) && (cz.size === 'share' || cz.size === 'hug')) {
      afterSize = cz.size
      if (typeof cz.grow === 'number') afterGrow = cz.grow
    }
    pushZone(
      <Comp
        key={czId}
        entityType={primary} entities={list} selectedId={selId} selected={sel}
        entitiesByType={ctx.entitiesByType} schema={ctx.schemas[primary]} viewState={view}
        onSelect={(id: string) => ctx.onSelect(primary, id)}
        onAdd={ctx.onAddFor ? () => ctx.onAddFor!(primary) : undefined}
        dispatch={ctx.dispatchCmd} assetUrl={ctx.assetUrl} uploadAsset={ctx.uploadAsset}
        statusOf={statusOf} statusFor={ctx.statusFor}
      />,
      cz.region,
    )
  })

  const injectedBefore: ReactNode[] | undefined = beforeZones.length ? beforeZones : undefined
  const injectedAfter: ReactNode[] | undefined = afterZones.length ? afterZones : undefined

  // 数据显示名(类型级 + 字段级)走 schema.label;无则各编辑器回退裸 id/key(语言中立,mod 透传)。
  const schema = ctx.schemas[primary]
  const typeLabel = schema?.label
  const columns = schema?.properties
    // asset_ref 是二进制资产引用,不是表格文本 —— 直渲会把 {"sha256":...} 原始 JSON 摊进单元格
    // (说明书工厂 M2 image-cleanup demo 实测)。表格自动列只留可读标量;资产走 asset/media/preview editor。
    ? Object.entries(schema.properties)
        .filter(([, fs]) => (fs as { type?: string }).type !== 'asset_ref')
        .map(([field, fs]) => ({
          field, label: (fs as { label?: string }).label ?? field,
        }))
    : undefined

  switch (ec) {
    case 'table':
      return (
        <TableEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          columns={columns}
          onSelect={(id) => ctx.onSelect(primary, id)}
          multiSelection={ctx.multiSelection?.[primary]}
          onSelectMulti={ctx.onSelectMulti ? (id, mode, orderedIds) => ctx.onSelectMulti!(primary, id, mode, orderedIds) : undefined}
          onAdd={ctx.onAddFor ? () => ctx.onAddFor!(primary) : undefined}
          dispatch={ctx.dispatchCmd} statusOf={statusOf}
          duplicateCommand={view.duplicateCommand as string | undefined}
          renameField={view.renameField as string | undefined}
        />
      )
    case 'form':
    case 'property': {
      // 属性面板(G-F 部分解冻):多型绑定 + view_state.followSelection 跟随跨类型最近选中;
      // 回退链在 resolveInspectorTarget(纯函数);follow=false 单型槽与旧路径字节等价。
      const target = resolveInspectorTarget(
        node.entity_bindings, view.followSelection === true,
        ctx.lastSelected ?? null, ctx.selection, ctx.entitiesByType,
      )
      // 空态:渲 FormEditor(entity=null)→ 属性面板外壳恒在 + 内容区空提示,**不再整槽塌成占位行**
      // (与 gallery/canvas2d 一致)。emptyTitle = 面板首绑类型 schema.label(回退类型 id)。
      if (!target) return (
        <FormEditor entity={null} controlId={typeof view.controlId === 'string' ? view.controlId : undefined} editorName={editorName} extraZones={injectedBefore} afterZones={injectedAfter} afterSize={afterSize} afterGrow={afterGrow} emptyTitle={schema?.label ?? primary}
                    dispatch={ctx.dispatchCmd} consumeActionResult={consumeActionResult} assetUrl={ctx.assetUrl}
                    viewStateScope={viewStateScope}
                    editorClass={ec === 'property' ? 'property' : 'form'} />
      )
      const aType = target.entityType
      const aSchema = ctx.schemas[aType]
      // 信息+控制单元(InfoControlUnit):sectionsByType[type] 优先,回退扁平 sections。
      // sections 与顶部 actions 互斥(声明 sections 则忽略顶部 actions,简单优先)。
      const sections = parseInspectorSections(
        (view.sectionsByType as Record<string, unknown> | undefined)?.[aType] ?? view.sections)
      // 动作条(ActionBarZone):actionsByType[type] 优先,回退扁平 actions(单型槽零迁移);sections 在时忽略。
      const actions = sections ? undefined : parseFormActions(
        (view.actionsByType as Record<string, unknown> | undefined)?.[aType] ?? view.actions)
      // 多选聚合编辑:该类型多选集 >1 → 传选区全集(active 在内,FormZone 聚合 + batch_update)。
      const multiIds = ctx.multiSelection?.[aType] ?? []
      const aList = ctx.entitiesByType[aType] ?? []
      const multiEntities = multiIds.length > 1
        ? multiIds.map((id) => aList.find((e) => e.id === id)).filter((e): e is NonNullable<typeof e> => !!e)
        : undefined
      // variants 单元:按 childType 桶 + childParentField===active.id 过滤出本实体的副本/候选,
      // 注入 FormEditor(InfoControlUnit 不直读 store,装配层喂数据 —— 同 detail editor 惯例)。
      let variantChildren: Record<string, typeof aList> | undefined
      for (const s of sections ?? []) {
        if (s.kind === 'variants' && s.childType && s.childParentField) {
          const bucket = ctx.entitiesByType[s.childType] ?? []
          const link = s.childParentField
          ;(variantChildren ??= {})[s.id] = bucket.filter((e) => e.data[link] === target.entity.id)
        }
      }
      return (
        <FormEditor
          controlId={typeof view.controlId === 'string' ? view.controlId : undefined}
          entity={target.entity} schema={aSchema} dispatch={ctx.dispatchCmd}
          consumeActionResult={consumeActionResult}
          editorName={editorName}
          viewStateScope={viewStateScope}
          extraZones={injectedBefore} afterZones={injectedAfter} afterSize={afterSize} afterGrow={afterGrow}
          editorClass={ec === 'property' ? 'property' : 'form'}
          status={ctx.statusFor ? ctx.statusFor(aType, target.entity.id) : undefined}
          typeLabel={node.entity_bindings.length > 1 ? aSchema?.label ?? aType : undefined}
          actions={actions}
          sections={sections}
          autoSections={view.autoSections === true}
          assetUrl={ctx.assetUrl}
          uploadAsset={ctx.uploadAsset}
          variantChildren={variantChildren}
          entityRefOptions={buildEntityRefOptions(aSchema, ctx.entitiesByType)}
          entities={multiEntities && multiEntities.length > 1 ? multiEntities : undefined}
        />
      )
    }
    case 'gallery':
    case 'cardlist': {
      // 导入缝(gap4):mod 声明 importCommand → 选文件 → uploadAsset → dispatch importCommand。
      // 复用 canvas2d 同一 ctx.uploadAsset 缝;payload 字段缺省 'image'(同 canvas2d import 约定)。
      const galImportCommand = view.importCommand as string | undefined
      const galPayloadField = (typeof view.importPayloadField === 'string' ? view.importPayloadField : 'image')
      const onGalImport = galImportCommand && ctx.uploadAsset
        ? async (file: File) => {
            const ref = await ctx.uploadAsset!(new Uint8Array(await file.arrayBuffer()),
                                               file.type || 'application/octet-stream', _extOf(file.name))
            const id = `${primary}-${(globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)).slice(0, 8)}`
            ctx.dispatchCmd(galImportCommand, { [galPayloadField]: ref, name: file.name, id })
          }
        : undefined
      return (
        <GalleryEditor
          editorClass={ec === 'cardlist' ? 'cardlist' : 'gallery'}
          editorName={editorName}
          extraZones={injectedBefore} afterZones={injectedAfter} afterSize={afterSize} afterGrow={afterGrow}
          entityType={primary} typeLabel={typeLabel} entities={list} selectedId={selId}
          onImport={onGalImport}
          // 新建入口:mod 用 view_state.allowAdd 显式开(与 importCommand/reorderCommand 同为选配缝),
          // 不给全舰队所有卡片网格凭空长按钮。写路径仍是框架的 entity.add + newEntityDefaults。
          onAdd={view.allowAdd === true && ctx.onAddFor ? () => ctx.onAddFor!(primary) : undefined}
          emptyTextKey={typeof view.emptyTextKey === 'string' ? view.emptyTextKey : undefined}
          acceptedMime={typeof view.acceptedMime === 'string' ? view.acceptedMime : undefined}
          onSelect={(id) => ctx.onSelect(primary, id)}
          multiSelection={ctx.multiSelection?.[primary]}
          onSelectMulti={ctx.onSelectMulti ? (id, mode, orderedIds) => ctx.onSelectMulti!(primary, id, mode, orderedIds) : undefined}
          assetUrl={ctx.assetUrl}
          thumbField={view.thumbField as string | undefined}
          scoreField={view.scoreField as string | undefined}
          labelField={view.labelField as string | undefined}
          descriptionField={view.descriptionField as string | undefined}
          summaryFields={Array.isArray(view.summaryFields) ? view.summaryFields.filter((x): x is string => typeof x === 'string') : undefined}
          fieldSchemas={schema?.properties}
          layoutMode={asGalleryLayoutMode(view.layoutMode)}
          // 卡片列表抽象拓展点(SPEC §3.5):筛选/动作/缩略图档位/逐卡显隐锁定/无媒体区
          searchFields={Array.isArray(view.searchFields) ? view.searchFields.filter((x): x is string => typeof x === 'string') : undefined}
          requiredFilters={parseRequiredCardFilters(view.requiredFilters ?? view.staticFilters)}
          filters={parseGridFilters(view.filters)}
          sorts={parseCardSorts(view.sorts)}
          defaultSort={typeof view.defaultSort === 'string' ? view.defaultSort : undefined}
          actions={parseGalleryActions(view.actions)}
          cardSize={asCardSize(view.cardSize)}
          viewStateScope={viewStateScope}
          visibilityField={view.visibilityField as string | undefined}
          lockedField={view.lockedField as string | undefined}
          showThumb={typeof view.showThumb === 'boolean' ? view.showThumb : undefined}
          // 闭合 onAdopt seam:mod 在 layout view_state 声明 adoptCommand,core 只路由派发
          onAdopt={view.adoptCommand ? (id) => ctx.dispatchCmd(view.adoptCommand as string, { type: primary, id }) : undefined}
          deleteCommand={view.deleteCommand as string | undefined}
          // 逐卡复制命令(mod 声明 duplicateCommand,如 entity.duplicate);设了则动作区渲「复制」选中项
          duplicateCommand={view.duplicateCommand as string | undefined}
          // 拖拽重排命令(mod 声明 reorderCommand,语义在 mod operation)
          reorderCommand={view.reorderCommand as string | undefined}
          // withParams 动作(批量生成)的参数小窗据命令 params_schema 驱动
          commandSpecs={ctx.commandSpecs}
          // AI 路线门控:requires_route='image' 的生图动作未绑凭据自动置灰(加性)
          aiRoutes={ctx.aiRoutes}
          // AI 内容合规门控:requires_route 的动作未接受条款时拦首击弹条款(加性)
          aiConsent={ctx.aiConsent}
          statusOf={statusOf} dispatch={ctx.dispatchCmd}
        />
      )
    }
    case 'task':
      return (
        <TaskEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          onSelect={(id) => ctx.onSelect(primary, id)}
          // 闭合 onRun seam:mod 声明 runCommand(运行/重跑某任务实体),core 只路由
          onRun={view.runCommand ? (id) => ctx.dispatchCmd(view.runCommand as string, { type: primary, id }) : undefined}
          stagesField={view.stagesField as string | undefined}
          statusOf={statusOf} dispatch={ctx.dispatchCmd}
        />
      )
    case 'canvas2d': {
      // spatial 模式:mod 在 view_state 声明几何字段映射(geometryField | xField/yField/wField/hField)才开
      const geometry = parseGeometryFieldMap(view)
      const pixelLayerField = view.pixelLayerField as string | undefined
      const selectionLayerField = view.selectionLayerField as string | undefined
      const maskField = view.maskField as string | undefined
      const rangeMaskField = view.rangeMaskField as string | undefined
      const effectivePixelLayerField = pixelLayerField ?? selectionLayerField ?? maskField ?? rangeMaskField
      const pixelLayerCommand = (view.pixelLayerCommand ?? view.maskCommand) as string | undefined
      const importCommand = view.importCommand as string | undefined
      // W1a 像素层提交缝:涂抹结束的整张 PNG → uploadAsset → dispatch mod 命令(JSON 带 ref)。
      const onPixelLayerCommit = effectivePixelLayerField && pixelLayerCommand && ctx.uploadAsset && sel
        ? async (bytes: Uint8Array) => {
            const ref = await ctx.uploadAsset!(bytes, 'image/png', '.png')
            ctx.dispatchCmd(pixelLayerCommand, {
              id: sel.id,
              image: ref,
              layer: ref,
              pixelLayer: ref,
              selectionLayer: ref,
              mask: ref,
              field: effectivePixelLayerField,
            })
          }
        : undefined
      // 导图缝:拖入文件 → uploadAsset → dispatch import 命令(声明 importCommand 时优先;否则回退旧 onDropFileFor)。
      const onDropFile = importCommand && ctx.uploadAsset
        ? async (file: File) => {
            const ref = await ctx.uploadAsset!(new Uint8Array(await file.arrayBuffer()),
                                               file.type || 'image/png', _extOf(file.name))
            const id = sel?.id ?? `${primary}-${(globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)).slice(0, 8)}`
            if (!sel) ctx.onSelect(primary, id)
            ctx.dispatchCmd(importCommand, { image: ref, name: file.name, id })
          }
        : ctx.onDropFileFor ? (file: File) => ctx.onDropFileFor!(primary, file) : undefined
      // layered 实体接缝(1.27.0):layerType → layer 实体 + 全局 selection 注入。
      // **这就是「编辑器只与唯一真相源通信」的物理实现**:canvas 与卡片列表(gallery)都只
      // 读写 useInteractionStore.selection[layerType] 与实体 store,互不知晓对方存在。
      const layerType = typeof view.layerType === 'string' ? view.layerType : undefined
      const anchorList = layerType && primary !== layerType ? list : []
      const layeredHost = layerType ? (anchorList[0] ?? null) : sel
      return (
        <Canvas2dEditor
          editorName={editorName}
          viewStateScope={viewStateScope}
          extraZones={injectedBefore} afterZones={injectedAfter} afterSize={afterSize} afterGrow={afterGrow}
          entity={layeredHost} assetUrl={ctx.assetUrl}
          status={ctx.statusFor && layeredHost ? ctx.statusFor(layeredHost.type, layeredHost.id) : undefined}
          bgField={view.bgField as string | undefined}
          preset={view.preset as never}
          features={view.features as never}
          tools={view.tools as never}   // 模板默认不传 → 纯视口(无 SAM 工具栏)
          layerType={layerType}
          layerEntities={layerType ? ctx.entitiesByType[layerType] ?? [] : undefined}
          activeLayerId={layerType ? ctx.selection[layerType] ?? null : undefined}
          onSelectLayer={layerType ? (id) => ctx.onSelect(layerType, id) : undefined}
          pixelLayerField={pixelLayerField}
          selectionLayerField={selectionLayerField}
          maskField={maskField}
          rangeMaskField={rangeMaskField}
          onPixelLayerCommit={onPixelLayerCommit}
          onDropFile={onDropFile}
          brushOpacity={typeof view.brushOpacity === 'number' ? view.brushOpacity : undefined}
          brushFlow={typeof view.brushFlow === 'number' ? view.brushFlow : undefined}
          brushSpacing={typeof view.brushSpacing === 'number' ? view.brushSpacing : undefined}
          dispatch={ctx.dispatchCmd}
          uploadAsset={ctx.uploadAsset}
          actionCommand={view.actionCommand as string | undefined}
          actionLabelKey={view.actionLabelKey as string | undefined}
          actionLabel={view.actionLabel as string | undefined}
          layerAddCommand={view.layerAddCommand as string | undefined}
          paintCommand={view.paintCommand as string | undefined}
          exportCommand={view.exportCommand as string | undefined}
          geometry={geometry}
          typeLabel={typeLabel}
          entities={geometry ? list : undefined}
          selectedId={selId}
          // F2 跨面板高亮通道:spatial 模式对 highlights[primary] 集合内实体画可区分描边(Rendering Hints)
          highlightedIds={ctx.highlights?.[primary]}
          onSelect={(id) => ctx.onSelect(primary, id)}
          constraints={parseConstraints(view)}
          addCommand={view.addCommand as string | undefined}
          // opt-in:画框后收一段文本(如意图)再 dispatch addCommand(如 object.add_by_box);缺 label = 旧行为立即建框。
          addPromptLabel={view.addPromptLabel as string | undefined}
          addPromptField={view.addPromptField as string | undefined}
          addPromptPlaceholder={view.addPromptPlaceholder as string | undefined}
          // bbox/几何拖拽更新命令:规范键名是 bboxUpdateCommand(types + SPEC),兼容旧 updateCommand。
          // 缺此回退则声明 bboxUpdateCommand 的 slot(如 object-generator-v3)拖框会绕过自定义命令走内置 entity.update,派生 bbox 字段失同步。
          updateCommand={(view.bboxUpdateCommand ?? view.updateCommand) as string | undefined}
          deleteCommand={view.deleteCommand as string | undefined}
          statusOf={statusOf}
        />
      )
    }
    case 'scene3d':
      return (
        <Scene3dEditor
          entityType={primary}
          editorName={editorName}
          entity={sel}
          entities={list}
          selectedId={selId}
          multiSelection={ctx.multiSelection}
          onSelectMulti={ctx.onSelectMulti}
          entitiesByType={ctx.entitiesByType}
          viewState={view}
          assetUrl={ctx.assetUrl}
          renderer={(typeof view.rendererKey === 'string' ? ctx.renderers?.[view.rendererKey] : undefined) ?? ctx.renderers?.scene3d}
          // 懒渲染器:mod 在 view_state 声明 rendererModuleId,core 按需拉取(不打包 three)
          rendererModuleId={view.rendererModuleId as string | undefined}
          status={ctx.statusFor && sel ? ctx.statusFor(primary, sel.id) : undefined}
          meshField={view.meshField as string | undefined}
          viewStateScope={viewStateScope}
          cameraField={view.cameraField as string | undefined}
          cameraMode={view.cameraMode === 'personal' || view.cameraMode === 'domain' ? view.cameraMode : undefined}
          selectionField={view.selectionField as string | undefined}
          displayModeField={view.displayModeField as string | undefined}
          displayModes={Array.isArray(view.displayModes) ? view.displayModes as never : undefined}
          controls={view.controls && typeof view.controls === 'object' && !Array.isArray(view.controls)
            ? {
                resetView: (view.controls as Record<string, unknown>).resetView === true,
                wireframe: (view.controls as Record<string, unknown>).wireframe === true,
              }
            : undefined}
          transformMode={view.transformMode as string | undefined}
          cameraCommand={view.cameraCommand as string | undefined}
          selectionCommand={view.selectionCommand as string | undefined}
          displayModeCommand={view.displayModeCommand as string | undefined}
          transformCommand={view.transformCommand as string | undefined}
          onSelectEntity={(entityType, id) => ctx.onSelect(entityType, id)}
          statusOf={ctx.statusFor}
          dispatch={ctx.dispatchCmd}
        />
      )
    case 'asset':
      return (
        <AssetEditor
          entity={sel} editorName={editorName} assetUrl={ctx.assetUrl}
          status={ctx.statusFor && sel ? ctx.statusFor(primary, sel.id) : undefined}
          assetField={view.assetField as string | undefined}
          transformCommand={view.transformCommand as string | undefined}
          dispatch={ctx.dispatchCmd}
        />
      )
    case 'media':
      return (
        <MediaEditor
          entity={sel} editorName={editorName} assetUrl={ctx.assetUrl}
          status={ctx.statusFor && sel ? ctx.statusFor(primary, sel.id) : undefined}
          mediaField={view.mediaField as string | undefined}
          fps={typeof view.fps === 'number' ? view.fps : undefined}
          markCommand={view.markCommand as string | undefined}
          dispatch={ctx.dispatchCmd}
          // C0 共享时间游标(首个消费者):scrub/播放 → setCurrentTime;别处改 currentTime → media seek。
          // ctx 未注入(装配层未接共享时间轴)则两端皆 undefined,退回纯本地播放。
          currentTime={ctx.currentTime}
          onCurrentTimeChange={ctx.setCurrentTime}
        />
      )
    case 'chart':
      return (
        <ChartEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          onSelect={(id) => ctx.onSelect(primary, id)}
          chartKind={view.chartKind as ChartKind | undefined}
          xField={view.xField as string | undefined}
          yField={view.yField as string | undefined}
          labelField={view.labelField as string | undefined}
          metricField={view.metricField as string | undefined}
          statusOf={statusOf}
        />
      )
    case 'pipeline':
      return (
        <PipelineEditor
          editorName={editorName}
          entityType={primary}
          entities={list}
          selectedId={selId}
          selected={sel}
          viewState={view}
          pipelineRuns={ctx.pipelineRuns ?? []}
          commandSpecs={ctx.commandSpecs ?? []}
          pipelineSpecs={ctx.pipelineSpecs ?? []}
          entitiesByType={ctx.entitiesByType}
          statusOf={ctx.statusFor}
          assetUrl={ctx.assetUrl}
          schemas={ctx.schemas}
          uploadAsset={ctx.uploadAsset}
          dispatch={ctx.dispatchCmd}
          onSelect={(id) => ctx.onSelect(primary, id)}
        />
      )
    case 'timeline':
      // 多实体时序:track/start/duration 字段映射成「轨道 × 时间」;新增/改/删走 mod 声明的命令
      // (addCommand=clip.add 仿 box.add;updateCommand/deleteCommand 缺省=内置 entity.update/delete)。
      // C0 共享时间游标(第二个消费者,与 media 同轴):scrub → setCurrentTime;别处改 currentTime → 播放头跟随。
      // unitsPerSecond(或 fps)把抽象 unit 映射成秒(缺省 1);ctx 未注入 → 退回本地 view-only 播放头。
      // dynamicTracks/rangeCreate 都严格 opt-in；maxTracks 是显式容量配置，未声明仍有框架有限默认值。
      return (
        <TimelineEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          onSelect={(id) => ctx.onSelect(primary, id)}
          fields={parseTimelineFields(view)}
          tracks={typeof view.tracks === 'number' ? view.tracks : undefined}
          maxTracks={typeof view.maxTracks === 'number' ? view.maxTracks : undefined}
          dynamicTracks={view.dynamicTracks === true}
          rangeCreate={view.rangeCreate === true}
          snap={typeof view.snap === 'number' ? view.snap : undefined}
          unitsPerSecond={typeof view.unitsPerSecond === 'number' ? view.unitsPerSecond : (typeof view.fps === 'number' ? view.fps : undefined)}
          currentTime={ctx.currentTime}
          onCurrentTimeChange={ctx.setCurrentTime}
          addCommand={view.addCommand as string | undefined}
          updateCommand={view.updateCommand as string | undefined}
          deleteCommand={view.deleteCommand as string | undefined}
          statusOf={statusOf} dispatch={ctx.dispatchCmd}
        />
      )
    case 'keyframe-curve':
      // 数值×时间曲线:关键帧 entity 映射 time(x)/value(y)/ease;与 timeline 并列、共享 currentTime(秒)。
      // 值域 valueMin/valueMax(缺省按数据自适应);snap/unitsPerSecond 同 timeline 语义。
      return (
        <CurveEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          onSelect={(id) => ctx.onSelect(primary, id)}
          fields={{
            time: typeof view.timeField === 'string' ? view.timeField : 'time',
            value: typeof view.valueField === 'string' ? view.valueField : 'value',
            ease: typeof view.easeField === 'string' ? view.easeField : undefined,
          }}
          valueMin={typeof view.valueMin === 'number' ? view.valueMin : undefined}
          valueMax={typeof view.valueMax === 'number' ? view.valueMax : undefined}
          snap={typeof view.snap === 'number' ? view.snap : undefined}
          unitsPerSecond={typeof view.unitsPerSecond === 'number' ? view.unitsPerSecond : (typeof view.fps === 'number' ? view.fps : undefined)}
          currentTime={ctx.currentTime}
          addCommand={view.addCommand as string | undefined}
          updateCommand={view.updateCommand as string | undefined}
          deleteCommand={view.deleteCommand as string | undefined}
          statusOf={statusOf} dispatch={ctx.dispatchCmd}
        />
      )
    case 'text': {
      // 单实体长文:编辑 view_state.field(缺省 body)长文本字段,draft→blur→entity.update CAS。
      if (!sel) return <div className="ze-slot__empty">{t('slot.selectToEdit', { primary })}</div>
      return (
        <TextEditor entity={sel} editorName={editorName} field={view.field as string | undefined} dispatch={ctx.dispatchCmd}
          status={ctx.statusFor ? ctx.statusFor(primary, sel.id) : undefined} />
      )
    }
    case 'code': {
      // 单实体代码:codeField/langField 字段,行号 + 等宽 + 语言标签。
      if (!sel) return <div className="ze-slot__empty">{t('slot.selectToEdit', { primary })}</div>
      return (
        <CodeEditor entity={sel} editorName={editorName} codeField={view.codeField as string | undefined}
          langField={view.langField as string | undefined} dispatch={ctx.dispatchCmd}
          status={ctx.statusFor ? ctx.statusFor(primary, sel.id) : undefined} />
      )
    }
    case 'graph':
      // 多实体节点图:x/y 自由定位 + deps 连边;选/拖/双击新增(addCommand)/Del 删除。
      return (
        <GraphEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          onSelect={(id) => ctx.onSelect(primary, id)}
          fields={parseGraphFields(view)}
          addCommand={view.addCommand as string | undefined}
          updateCommand={view.updateCommand as string | undefined}
          deleteCommand={view.deleteCommand as string | undefined}
          statusOf={statusOf} dispatch={ctx.dispatchCmd}
        />
      )
    case 'review': {
      // 单实体审阅:approve/reject 写 statusField + 批注 noteField,皆走 entity.update CAS。
      // W4 前后对比(opt-in):声明 beforeField/afterField(asset 字段)→ 顶部渲 CompareFrame;
      // showDecision:false(纯对比,无审阅决策,如 cleanup 候选)隐藏 approve/reject + 批注。
      if (!sel) return <div className="ze-slot__empty">{t('slot.selectToEdit', { primary })}</div>
      const cmpUrl = (f?: string) => {
        const r = f ? asAssetRef(sel.data[f]) : null
        return r && ctx.assetUrl ? ctx.assetUrl(r) : undefined
      }
      return (
        <ReviewEditor entity={sel} editorName={editorName} statusField={view.statusField as string | undefined}
          noteField={view.noteField as string | undefined} dispatch={ctx.dispatchCmd}
          beforeUrl={cmpUrl(view.beforeField as string | undefined)}
          afterUrl={cmpUrl(view.afterField as string | undefined)}
          compareMode={view.compareMode as never}
          showDecision={view.showDecision as boolean | undefined}
          status={ctx.statusFor ? ctx.statusFor(primary, sel.id) : undefined} />
      )
    }
    case 'compare': {
      // 单实体前后对比 viewer(一等公民,非 review):只解析 before/after 资产 → CompareFrame,
      // 无 approve/reject/批注。view_state:beforeField/afterField/mode(slider|side-by-side|onion)。
      if (!sel) return <div className="ze-slot__empty">{t('slot.selectToEdit', { primary })}</div>
      const cmpUrl = (f?: string) => {
        const r = f ? asAssetRef(sel.data[f]) : null
        return r && ctx.assetUrl ? ctx.assetUrl(r) : undefined
      }
      return (
        <CompareEditor entity={sel} editorName={editorName}
          beforeUrl={cmpUrl(view.beforeField as string | undefined)}
          afterUrl={cmpUrl(view.afterField as string | undefined)}
          mode={(view.mode ?? view.compareMode) as never}
          status={ctx.statusFor ? ctx.statusFor(primary, sel.id) : undefined} />
      )
    }
    case 'outline':
      // 大纲/层级树(闭 G-C):多实体 master,按 parentField/orderField 渲单父层级树,选择驱动。
      // 展开/折叠是编辑器本地视图态;字段映射经 view_state。控制命令(actions)待真实消费者再接
      // 通用 ToolbarAction parser(当前无消费者,核心是树+选择)。
      return (
        <OutlineEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          onSelect={(id) => ctx.onSelect(primary, id)}
          multiSelection={ctx.multiSelection?.[primary]}
          onSelectMulti={ctx.onSelectMulti ? (id, mode, orderedIds) => ctx.onSelectMulti!(primary, id, mode, orderedIds) : undefined}
          statusOf={statusOf}
          dispatch={ctx.dispatchCmd}
          parentField={view.parentField as string | undefined}
          orderField={view.orderField as string | undefined}
          labelField={view.labelField as string | undefined}
          iconField={view.iconField as string | undefined}
          thumbField={view.thumbField as string | undefined}
          badgeField={view.badgeField as string | undefined}
          visibilityField={view.visibilityField as string | undefined}
          lockedField={view.lockedField as string | undefined}
          renameField={view.renameField as string | undefined}
          deleteCommand={view.deleteCommand as string | undefined}
          duplicateCommand={view.duplicateCommand as string | undefined}
          assetUrl={ctx.assetUrl}
          actions={parseToolbarActions(view.actions)}
        />
      )
    case 'html':
      // HTML 内容编辑器(promote webPreview):沙盒 iframe 预览 + 源码;沙盒策略住 core 不可配。
      // 单实体(选中驱动,回退单实体);htmlField/applyCommand 经 view_state。
      if (!sel) return <div className="ze-slot__empty">{t('slot.selectToEdit', { primary })}</div>
      return (
        <HtmlEditor
          entity={sel} editorName={editorName}
          htmlField={view.htmlField as string | undefined}
          applyCommand={view.applyCommand as string | undefined}
          dispatch={ctx.dispatchCmd}
          status={ctx.statusFor ? ctx.statusFor(primary, sel.id) : undefined}
        />
      )
    case 'dialog':
    case 'conversation':
      // AI 对话:message 实体集 → transcript 气泡 + 发送(entity.add user 消息 + 可选 replyCommand)。
      // 多实体 master(entitiesByType[primary] 即消息集);字段映射经 view_state。
      // secondary target 接缝:如 Chat Canvas 用 message 驱动 page.generate,layout 可声明
      // replyTargetType/pageId/prompt 映射,core 统一组装 replyArgs + messageExtraData。
      const replyTargetType = typeof view.replyTargetType === 'string' ? view.replyTargetType : undefined
      const replyTarget = replyTargetType
        ? ((ctx.selection[replyTargetType]
            ? (ctx.entitiesByType[replyTargetType] ?? []).find((e) => e.id === ctx.selection[replyTargetType])
            : null)
          ?? (ctx.entitiesByType[replyTargetType] ?? []).find((e) => !e.deleted)
          ?? null)
        : null
      const replyTargetIdArg = typeof view.replyTargetIdArg === 'string' ? view.replyTargetIdArg : 'id'
      const messageLinkField = typeof view.messageLinkField === 'string' ? view.messageLinkField : undefined
      const replyArgs = {
        ...(asRecord(view.replyArgs) ?? {}),
        ...(replyTarget ? { [replyTargetIdArg]: replyTarget.id } : {}),
      }
      const messageExtraData = messageLinkField && replyTarget ? { [messageLinkField]: replyTarget.id } : undefined
      // 编辑目标接缝:view_state 声明 replyScopeFromSelection: '<entityType>' 时,composer 上方
      // 常驻「编辑目标」chips(实时跟随该类型的选中,发送前所见即所确认),发送时刻把选中作为
      // args.scopeIds 随 replyCommand 下发;点 × 清空选择。会话选择是 Interaction State(非领域态)。
      const scopeType = typeof view.replyScopeFromSelection === 'string' ? view.replyScopeFromSelection : undefined
      const scopeIdsOf = () => {
        if (!scopeType) return [] as string[]
        const multi = ctx.multiSelection?.[scopeType] ?? []
        const single = ctx.selection[scopeType]
        return multi.length ? multi : (single ? [single] : [])
      }
      const scopeIdsNow = scopeIdsOf()
      const replyArgsLive = scopeType
        ? () => { const ids = scopeIdsOf(); return ids.length ? { scopeIds: ids } : {} }
        : undefined
      const replyScope = scopeType ? {
        label: t('editor.replyScope'),
        emptyLabel: t('editor.replyScopeAll'),
        clearLabel: t('editor.clearSelection'),
        names: scopeIdsNow.map((id) => (ctx.entitiesByType[scopeType] ?? []).find((e) => e.id === id)?.name || id),
        onClear: () => { ctx.onSelectMulti?.(scopeType, '', 'none', []); ctx.onSelect(scopeType, null) },
      } : undefined
      return (
        <DialogEditor
          editorClass={ec === 'conversation' ? 'conversation' : 'dialog'}
          editorName={editorName}
          entityType={primary} typeLabel={typeLabel} entities={list}
          roleField={view.roleField as string | undefined}
          textField={view.textField as string | undefined}
          orderField={view.orderField as string | undefined}
          userRole={view.userRole as string | undefined}
          replyCommand={view.replyCommand as string | undefined}
          replyTextArg={view.replyTextArg as string | undefined}
          replyArgs={Object.keys(replyArgs).length ? replyArgs : undefined}
          replyArgsLive={replyArgsLive}
          replyScope={replyScope}
          messageExtraData={messageExtraData}
          status={replyTarget && ctx.statusFor ? ctx.statusFor(replyTarget.type, replyTarget.id) : (ctx.statusFor && sel ? ctx.statusFor(primary, sel.id) : undefined)}
          extraZones={injectedBefore}
          afterZones={injectedAfter}
          afterSize={afterSize}
          afterGrow={afterGrow}
          dispatch={ctx.dispatchCmd}
        />
      )
    case 'document':
      // 块文档(Notion/Obsidian/飞书 式):多实体 master,block 实体(parent/order 单父树)渲成文档流,
      // 每块经块注册表 → L2 原子(prose);Notion 式逐块内联编辑(聚焦源/失焦渲染),选择驱动 selection[block]。
      // 字段映射经 view_state;onAdd 走框架 onAddFor 接缝(newEntityDefaults)。
      return (
        <DocumentEditor
          entityType={primary} editorName={editorName} typeLabel={typeLabel} entities={list} selectedId={selId}
          onSelect={(id) => ctx.onSelect(primary, id)}
          multiSelection={ctx.multiSelection?.[primary]}
          onSelectMulti={ctx.onSelectMulti ? (id, mode, orderedIds) => ctx.onSelectMulti!(primary, id, mode, orderedIds) : undefined}
          onAdd={ctx.onAddFor ? () => ctx.onAddFor!(primary) : undefined}
          statusOf={statusOf}
          dispatch={ctx.dispatchCmd}
          blockTypeField={view.blockTypeField as string | undefined}
          textField={view.textField as string | undefined}
          parentField={view.parentField as string | undefined}
          orderField={view.orderField as string | undefined}
          mediaField={view.mediaField as string | undefined}
        />
      )
    case 'custom': {
      // mod 注入式自定义面板:view_state.rendererKey 指一个 mod 提供的组件(经 @mod/05-renderers
      // → ctx.renderers)。core 只把完整 slot 上下文透传,自身零业务渲染。任意 slot 通用,机制同
      // scene3d 的 renderer 注入但不限编辑器类型。组件 props 形如 CustomPaneProps(见 03-editor/types)。
      const key = view.rendererKey as string | undefined
      const Comp = key ? ctx.renderers?.[key] : undefined
      if (!Comp) return <div className="ze-slot__empty">{t('slot.rendererMissing', { key: key ?? '?' })}</div>
      return (
        <Comp
          entityType={primary}
          entities={list}
          selectedId={selId}
          selected={sel}
          entitiesByType={ctx.entitiesByType}
          schema={schema}
          viewState={view}
          onSelect={(id: string) => ctx.onSelect(primary, id)}
          onAdd={ctx.onAddFor ? () => ctx.onAddFor!(primary) : undefined}
          dispatch={ctx.dispatchCmd}
          assetUrl={ctx.assetUrl}
          uploadAsset={ctx.uploadAsset}
          statusOf={statusOf}
          statusFor={ctx.statusFor}
        />
      )
    }
    default:
      return <div className="ze-slot__empty">{t('slot.editorUnregistered', { ec })}</div>
  }
}
