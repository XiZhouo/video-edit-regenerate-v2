/**
 * FormEditor — `form` editor class (M1)
 *
 * 记录型: 编辑 1 个 entity 的 data 字段。字段来自 schema.properties (有则) 或 data keys。
 * 受控件: entity 进, dispatch 出。本地 draft 暂存输入, onBlur 时若有变化 → entity.update。
 * entity prop 变化 (新 version, 经 SSE / dispatch 响应回灌) 时重置 draft。
 *
 * 写走 command dispatch (SPEC §2.4): entity.update args = {type, id, patches:{field:value},
 * expected_version}。patches 是 {field: new_value} 字典形态 (见 builtin entity.update)。
 *
 * 属性面板增强 (G-F 部分解冻, 全加性 props):
 *   - typeLabel: 多型跟随槽 (followSelection) 下 toolbar 标题显「类型 · 名称」
 *   - actions:   业务动作条 (ActionBarZone, manifest zone `actionbar`; FormAction 闭集)
 *   - entities:  多选聚合编辑选区 (length>1 → FormZone 聚合模式, 写回 entity.batch_update);
 *                toolbar count 槽显「已选 N」徽记
 */

import { type ReactNode } from 'react'
import { DisclosureStateProvider, useT, type ViewStateScope } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { ErrorPlaceholder, ProgressRing } from '../../lib/Skeleton'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { FormZone } from '../../zones/FormZone'
import { ActionBarZone } from '../../zones/ActionBarZone'
import { InfoControlUnit } from '../../zones/InfoControlUnit'
import { defaultSections } from '../../lib/defaultSections'
import { useScrollAffordance } from '../../lib/useScrollAffordance'
import { entityRunIsActive } from '../../lib/entityRunPresentation'
import type { FormAction, InspectorSectionSpec } from '../../zones/types'
import type {
  AssetRefDict, AssetUrlFn, DispatchFn, EntityRefOptions, EntityRunStatus, EntityTypeSchema,
  EntityDict, FormActionResultConsumer,
} from '../../types'

export type FormEditorProps = {
  /** 教学锚点 id:设了就在编辑器根上落 `data-ze-control="<id>"`,引导步的 `control:<id>` 便能指到本面板。
   *  与 GalleryAction.controlId 同一套语义 —— 教学讲的是位置,不是某个命令。 */
  controlId?: string
  /** null = 无选中:渲空骨架(外壳恒在,内容区空提示),不整槽塌成占位行。 */
  entity: EntityDict | null
  schema?: EntityTypeSchema
  dispatch?: DispatchFn
  consumeActionResult?: FormActionResultConsumer
  status?: EntityRunStatus   // 运行态装饰实体；仅空 pending 骨架，error 附着且保留真实字段
  /** 编辑器名(layout slot.name;多个槽绑同一类型时各自命名,优先于 typeLabel 回退)。 */
  editorName?: string
  /** 注入功能区(已实例化;装配层据 view_state.promptZone/customZones 构造):
   *  extraZones=content 之前(操作区尾),afterZones=content 之后(工作区底部)。 */
  extraZones?: ReactNode[]
  afterZones?: ReactNode[]
  afterSize?: 'hug' | 'share'
  afterGrow?: number
  /** 数据显示名(多型跟随槽下标题前缀「类型 · 名称」;语言中立,mod schema.label 透传)。 */
  typeLabel?: string
  /** 业务动作条(声明式 FormAction 闭集;经 view_state.actions / actionsByType 进)。 */
  actions?: FormAction[]
  /** 多选聚合编辑选区(含 active;length>1 → 聚合模式 + 徽记 + scope:'selection' 派发集)。 */
  entities?: EntityDict[]
  /** 信息+控制单元列表(声明式 InspectorSectionSpec 闭集;经 view_state.sections / sectionsByType
   *  进)。非空 → 属性面板渲为 N 个单元(顶部不再渲 actionbar,actions 与 sections 互斥,sections 优先);
   *  空 → 旧路径(actionbar + FormZone)字节等价。 */
  sections?: InspectorSectionSpec[]
  /** 自动排版(auto编排):未声明 sections 时由 schema+entity 派生默认单元(asset→preview/只读→readonly-kv/
   *  其余→fields)。opt-in(默认 false 走旧路径,向后兼容);经 view_state.autoSections 进。 */
  autoSections?: boolean
  /** preview/variants/asset kind 单元解析 asset_ref → URL(经 SlotContext 透传)。 */
  assetUrl?: AssetUrlFn
  /** asset kind 单元的上传缝:bytes → asset_ref(经 SlotContext 透传;缺省则该单元降级只读)。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  /** kind==='variants' 单元的子实体集(装配层按 section.id 预过滤:childType 桶里 childParentField===active.id)。 */
  variantChildren?: Record<string, EntityDict[]>
  /** entity_ref 字段候选表(按绑定类型分桶;装配层从 entitiesByType 预派生;透传给 FormZone/InfoControlUnit)。 */
  entityRefOptions?: EntityRefOptions
  /** 空态标题(无选中时 header 显示;装配层 slotAdapters 传面板首绑类型的 schema.label)。 */
  emptyTitle?: string
  /** 标准 public class 用 property;form 保留为低级字段/记录表单兼容名。 */
  editorClass?: 'form' | 'property'
  /** 个人折叠偏好的四轴作用域；缺失时仍可在本次挂载内工作，但不落盘。 */
  viewStateScope?: ViewStateScope
}

/** Durable disclosure namespace. Do not derive/localize or rename without a view-state migration. */
export const INSPECTOR_SECTION_DISCLOSURE_OWNER = 'inspector.sections'

export function FormEditor({ entity, schema, dispatch, consumeActionResult, status, editorName, extraZones, afterZones, afterSize, afterGrow, typeLabel, actions, entities, sections, autoSections, assetUrl, uploadAsset, variantChildren, entityRefOptions, emptyTitle, editorClass = 'form', viewStateScope, controlId }: FormEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const bodyRef = useScrollAffordance('y')

  // 空态(无选中):属性面板外壳(EditorFrame)**恒在** —— 与 gallery/canvas2d 一致(它们总渲 EditorFrame,
  // 空态在内容区);内容区显「选一个 X 编辑」提示,**绝不整槽塌成占位行**(SPEC §form「编辑器自包含」)。
  // 下方所有逻辑(name/title/version/sections…)都要求 entity 非空,故此分支必须在它们之前。
  if (!entity) {
    const label = editorName ?? emptyTitle ?? typeLabel ?? ''
    return (
      <EditorFrame editorClass={editorClass} dataAttrs={controlId ? { 'data-ze-control': controlId } : undefined} contentMode="pass-through" header={<HeaderZone title={label || undefined} count={undefined} />}>
        <EditorFrame.Empty>{t('slot.selectToEdit', { primary: label })}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const phase = status?.phase
  const runActive = entityRunIsActive(status)
  const name = entity.name || (runActive ? t('editor.generating') : entity.id.slice(0, 8))
  const title = editorName ? `${editorName} · ${name}` : (typeLabel ? `${typeLabel} · ${name}` : name)
  const multiCount = entities && entities.length > 1 ? entities.length : 0
  // 自动排版:声明了 sections 用之;否则 autoSections 开则由 schema+entity 派生(auto编排);都没有走旧路径。
  const effSections = (sections && sections.length > 0) ? sections : (autoSections ? defaultSections(schema, entity) : undefined)
  const isUnitPath = !!(effSections && effSections.length > 0)
  // 计数槽:活动 run 显进度环/转圈;否则显版本。「已选 N」徽记**仅旧聚合路径**显示 ——
  // 单元路径(sections)是单对象属性面板、字段编辑单实体,显多选徽记会误导(控制命令可 scope=selection
  // 批量,但属性主体是 active 单对象)。见 review nit:多选+sections 时不再误显「已选 N」。
  const baseCount: ReactNode = runActive
    ? (phase === 'running'
        ? <ProgressRing progress={status?.progress} size={18} label={status?.stage_label} />
        : <span className="ze-spinner" data-size="sm" />)
    : `v${entity.version}`
  const count: ReactNode = (multiCount > 0 && !isUnitPath)
    ? <span className="ze-badge" data-variant="info">{t('editor.nSelected', { n: multiCount })}</span>
    : baseCount

  const selectionIds = multiCount > 0 ? entities!.map((e) => e.id) : undefined

  // 信息+控制单元路径:声明 sections → 属性面板渲为 N 个单元(顶部 actionbar 互斥忽略)。
  if (isUnitPath) {
    return (
      // 单元路径:N 个 InfoControlUnit 各自是完整 raised 视觉块,故 content 走 pass-through(只留
      // flex/scroll,不再画 sunken 边界)→ unit 成唯一边界,消除 content + unit 双壳(截图 1/3)。
      <EditorFrame editorClass={editorClass} dataAttrs={controlId ? { 'data-ze-control': controlId } : undefined} contentMode="pass-through" header={<HeaderZone title={title} count={count} />}
        zones={extraZones?.length ? extraZones : undefined} afterZones={afterZones} afterSize={afterSize} afterGrow={afterGrow}>
        {/* 单元列收进滚动 body(复用 form-body 范式 flex:1+min-height:0+overflow:auto):
            编辑器矮于全部单元时单元列内部滚动,而非末单元越过 overflow:clip 被裁、控制区按钮够不到。
            [v2.1 功能区成块] ze-pad/ze-gap(space-2):单元块从 body 边内缩 + 块间留气
            (R2 删了单元间发丝线,块间距改由父列表 gap 承接;同 __zone margin 节奏)。 */}
        <DisclosureStateProvider scope={viewStateScope} ownerId={INSPECTOR_SECTION_DISCLOSURE_OWNER} contextId={entity.type}>
          <div ref={bodyRef} className="ze-editor__form-body ze-scroll-fade-y ze-pad ze-gap" data-p="1" data-gap="1" data-run-phase={phase}>
            {phase === 'error' && (
              <div className="ze-editor__form-status">
                <ErrorPlaceholder compact
                  error={status?.error}
                  onRetry={status?.run_id ? () => fire('pipeline.retry_failed', {
                    pipeline_id: status.run_id, item_ids: [entity.id],
                  }) : undefined}
                />
              </div>
            )}
            {effSections!.map((s) => (
              <InfoControlUnit
                key={s.id}
                section={s} entity={entity} schema={schema} status={status}
                selectionIds={selectionIds} assetUrl={assetUrl} uploadAsset={uploadAsset}
                childEntities={variantChildren?.[s.id]} entityRefOptions={entityRefOptions} dispatch={fire}
                consumeActionResult={consumeActionResult}
              />
            ))}
          </div>
        </DisclosureStateProvider>
      </EditorFrame>
    )
  }

  // 旧路径(actionbar + FormZone):FormZone 自带行级结构,内容区走 pass-through 去掉多余 sunken 外壳
  // (与单元路径一致;消除 .ze-editor → .ze-editor__content 双壳)。
  return (
    <EditorFrame
      editorClass={editorClass}
      dataAttrs={controlId ? { 'data-ze-control': controlId } : undefined}
      contentMode="pass-through"
      header={<HeaderZone title={title} count={count} />}
      zones={[
        (actions && actions.length > 0) ? (
          <ActionBarZone
            key="actionbar"
            actions={actions}
            entityType={entity.type}
            entity={entity}
            selectionIds={selectionIds}
            schema={schema}
            running={runActive}
            dispatch={fire}
            consumeActionResult={consumeActionResult}
          />
        ) : null,
        ...(extraZones ?? []),
      ].filter(Boolean) as ReactNode[]}
      afterZones={afterZones} afterSize={afterSize} afterGrow={afterGrow}
    >
      <FormZone entity={entity} schema={schema} dispatch={fire} status={status} entities={entities} entityRefOptions={entityRefOptions} />
    </EditorFrame>
  )
}
