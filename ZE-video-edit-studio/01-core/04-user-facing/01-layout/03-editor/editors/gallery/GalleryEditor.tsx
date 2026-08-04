/**
 * GalleryEditor — `gallery` editor class:多实体缩略图 / 候选 / 结果浏览(声明式组合功能区)。
 *
 * 本编辑器 = EditorFrame 外壳 + **三功能区**(对齐功能区统一模型):
 *   header(HeaderZone,身份/计数)
 *   → filterbar(FilterBarZone:搜索 + 状态 pill 筛选 —— 辅助查看,改 view_state 即时无 confirm)
 *   → control(选择簇 全选/反选/清空 + 导入 + 全局/选区命令 —— 触发命令,按同类动作成组换行)
 *   内容区 = GridZone(卡片网格)。
 * 卡片尺寸由 mod 预设 cardSize(compact/balanced/spacious)提供初始 scale,用户可用"缩略图尺寸"无级滑杆覆盖;
 * 内部计算 CSS 变量让 thumb.css 自适应列数,偏好存 localStorage,不写 Document/OpLog。无内联对比视图(对比走独立 compare 编辑器)。
 * 视图态(搜索/pill/排序/缩略图尺寸)走共享 scoped view-state store,不 dispatch;领域写(删/采纳/重排/批量命令)走 command dispatch;
 * 选择操作(全选/多选/加减选/反选/清空)走既有 selectionModel 八模式 + onSelectMulti,本编辑器只暴露可见入口。
 */

import { useMemo, useRef, useState, type ReactNode } from 'react'
import { useScopedViewState, useT, type ViewStateScope } from '@ze/core/04-user-facing/05-preferences'
import { BaseDialog } from '@ze/core/04-user-facing/01-layout/01-header/dialogs/BaseDialog'
import { useEditorDispatch } from '../../lib/dispatch'
import { useEditorShortcuts, type ShortcutSpec } from '../../lib/useEditorShortcuts'
import { resolveActionLabel, resolveActionConfirm } from '../../zones/actionGating'
import { ConfirmDialog } from '../../zones/ConfirmDialog'
import { AiRouteGate } from '../../../AiRouteGate'
import type { AiRoutesState } from '../../../../03-authentication/aiRoutes'
import { AiConsentGate } from '../../../AiConsentGate'
import type { AiConsentState } from '../../../../03-authentication/aiConsent'
import {
  filterCards, resolveSearchFields, sortCards, resolveDefaultSort, CARD_SIZE_TO_THUMB_SCALE,
  THUMB_SCALE_MIN, THUMB_SCALE_MAX, asThumbSize, clampThumbScale, thumbScaleForSize,
  type ActiveFilter, type CardSize,
} from '../../lib/cardListModel'
import {
  GALLERY_VIEW_STATE_SLICE, galleryViewStateCodec, type GalleryViewState,
} from '../../lib/galleryViewState'
import { paramProperties, buildStartDraft, missingRequiredParams } from '../../lib/pipelineConsole'
import { ParamPanel } from '../../primitives/ParamPanel'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { FilterBarZone } from '../../zones/FilterBarZone'
import { FunctionZone } from '../../zones/FunctionZone'
import { GridZone } from '../../zones/GridZone'
import { triggerDownload } from '../../lib/assetActions'
import type { CardSort, GalleryAction, GridFilter } from '../../zones/types'
import type { ThumbnailLayoutMode } from '../../primitives/ThumbnailGrid'
import type { SelectionMode } from '../../../lib/selectionModel'
import type { CommandSpecBrief } from '../../../02-footer'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus, EntityTypeSchema, FieldSchema } from '../../types'

/** 动作分组:mod 显式 `cluster` 优先(registry-driven,组序=首现顺序);缺省回退命令名推断闭集
 *  (generate/disposition/export/other/danger,推断组序固定)。同组渲进同一 .ze-row(组=不可拆换行单元)。 */
type GalleryActionGroup = 'generate' | 'disposition' | 'export' | 'other' | 'danger'
const GALLERY_ACTION_GROUP_ORDER: GalleryActionGroup[] = ['generate', 'disposition', 'export', 'other', 'danger']

function galleryActionGroup(action: GalleryAction): string {
  if (action.cluster) return action.cluster
  if (action.variant === 'danger' || action.command === 'entity.delete') return 'danger'
  if (action.command.includes('generate')) return 'generate'
  if (action.command.includes('disposition')) return 'disposition'
  if (action.command.includes('export')) return 'export'
  return 'other'
}

/** 分组序:显式 cluster 组按首现顺序在前,推断组按闭集固定顺序在后。
 *  显式/推断按 provenance(该组是否由 .cluster 声明)区分,不按组名字符串撞闭集判——
 *  否则 cluster:'danger' 这类与推断闭集重名的显式组会静默掉进固定序桶。 */
function orderedActionGroups(actions: GalleryAction[]): GalleryAction[][] {
  const byGroup = new Map<string, GalleryAction[]>()
  const declaredKeys = new Set<string>()
  for (const a of actions) {
    const g = galleryActionGroup(a)
    if (a.cluster) declaredKeys.add(g)
    const list = byGroup.get(g)
    if (list) list.push(a)
    else byGroup.set(g, [a])
  }
  const declared: GalleryAction[][] = []
  for (const [g, list] of byGroup) {
    if (declaredKeys.has(g)) declared.push(list)
  }
  const inferred = GALLERY_ACTION_GROUP_ORDER
    .map((g) => (declaredKeys.has(g) ? undefined : byGroup.get(g)))
    .filter((list): list is GalleryAction[] => !!list?.length)
  return [...declared, ...inferred]
}

function readThumbSizePreference(key: string | undefined, fallback: number): number {
  if (!key || typeof localStorage === 'undefined') return fallback
  try {
    const raw = localStorage.getItem(key)
    const legacy = asThumbSize(raw)
    if (legacy) return thumbScaleForSize(legacy)
    return raw === null ? fallback : clampThumbScale(raw, fallback)
  } catch {
    return fallback
  }
}

function writeThumbSizePreference(key: string | undefined, value: number): void {
  if (!key || typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(key, String(clampThumbScale(value)))
  } catch {
    /* localStorage may be unavailable in private mode. */
  }
}

export type GalleryEditorProps = {
  /** 标准 public class 用 cardlist;gallery 保留为兼容 alias。 */
  editorClass?: 'gallery' | 'cardlist'
  entityType: string
  /** 编辑器名(layout slot.name;多个槽绑同一类型时各自命名,优先于 typeLabel 回退)。 */
  editorName?: string
  /** 注入功能区(已实例化;装配层据 view_state.promptZone/customZones 构造):
   *  extraZones=content 之前(操作区尾),afterZones=content 之后(工作区底部)。 */
  extraZones?: ReactNode[]
  afterZones?: ReactNode[]
  /** afterZones 纵向尺寸(透 EditorFrame.afterSize/afterGrow):hug(默认)/share(与 content 比例瓜分)。 */
  afterSize?: 'hug' | 'share'
  afterGrow?: number
  /** 类型显示名(数据显示名,mod 经 schema.label 提供;无则回退裸 entityType)。 */
  typeLabel?: string
  entities: EntityDict[]
  selectedId?: string | null
  onSelect?: (id: string) => void
  /** 多选(P0):传了则卡片点击走 replace/⌘ toggle/⇧ range */
  multiSelection?: string[]
  onSelectMulti?: (id: string, mode: SelectionMode, orderedIds: string[]) => void
  assetUrl?: AssetUrlFn
  thumbField?: string
  scoreField?: string
  labelField?: string
  descriptionField?: string
  summaryFields?: string[]
  /** summaryFields 摘要 chip 字段 schema(透传给 GridZone 取人话 label;缺则回退裸字段键)。 */
  fieldSchemas?: Record<string, FieldSchema>
  layoutMode?: ThumbnailLayoutMode
  // ── 卡片列表抽象拓展点(SPEC §3.5;全可选,缺省=原 gallery 行为) ──────────────
  /** 开搜索框 + 覆盖字段(空数组=用 labelField/descriptionField 缺省;entity.name 恒参与)。 */
  searchFields?: string[]
  /** 固定过滤条件,例如同为 layer 时只显示 role=candidate/output。 */
  requiredFilters?: ActiveFilter[]
  /** 状态 pill 筛选组(values 缺省按字段去重派生)。 */
  filters?: GridFilter[]
  /** 排序下拉(纯视图态,不 dispatch):声明式字段映射 + 比较类型。 */
  sorts?: CardSort[]
  /** 默认排序 id;缺省取 sorts[0]。 */
  defaultSort?: string
  /** 动作区(mod 声明;selection 范围对当前选中派发,global 对整列表)。 */
  actions?: GalleryAction[]
  /** 卡片尺寸预设(mod 静态默认;缺省 balanced)。用户滑杆可覆盖为本地视图偏好。 */
  cardSize?: CardSize
  /** localStorage key for per-project/per-slot thumb size preference; absent = memory-only. */
  thumbSizePreferenceKey?: string
  /** 共享个人视图态作用域;传入后搜索/筛选/排序/缩略图统一版本化持久。 */
  viewStateScope?: ViewStateScope
  /** 逐卡显隐/锁定 boolean data 字段(走内置 entity.update CAS)。 */
  visibilityField?: string
  lockedField?: string
  /** false = 无媒体区一等公民(纯文本卡;缺省 layoutMode 落 card-list)。 */
  showThumb?: boolean
  statusOf?: (id: string) => EntityRunStatus | undefined
  onAdopt?: (id: string) => void
  deleteCommand?: string
  /** 逐卡复制命令 id(mod 声明,如 `entity.duplicate {type,id}`);设了则动作区渲「复制」选中项,
   *  对每张选中卡逐个 dispatch {type,id,expected_version}(源只读、复制忽略 CAS)。无则不渲复制。 */
  duplicateCommand?: string
  /** 拖拽重排命令 id(mod 声明,如 `<type>.reorder {type,id,before_id}`);无则禁拖拽 */
  reorderCommand?: string
  /** 导入缝(gap4):传了则渲「导入」入口,选文件 → onImport(由 slotAdapters 上传→派发 importCommand);无则不渲。 */
  onImport?: (file: File) => void | Promise<void>
  /** 新建缝:传了则渲「新增」入口 → onAdd(slotAdapters 接 ctx.onAddFor → entity.add + newEntityDefaults)。
   *  与 Table/Document 的新增入口同形(labelKey editor.add)。mod 经 view_state.allowAdd 显式开启 ——
   *  没有手动新建入口的卡片网格,唯一的条目生产者就只剩 AI,违反「无 AI 时 100% 可用」。 */
  onAdd?: () => void
  /** 空态文案的 i18n key(mod 声明,如 'mod.gallery.empty');无则用框架缺省「暂无 {type}」。
   *  缺省文案只说「没有」,不说「怎么才能有」—— 空列表正是最需要指路的时刻。 */
  emptyTextKey?: string
  /** 文件选择器 accept(如 'image/*');缺省 image/*。 */
  acceptedMime?: string
  /** 命令名册(含 params_schema):withParams 动作的参数小窗据此驱动。 */
  commandSpecs?: CommandSpecBrief[]
  /** AI image 路线态:动作命令声明 requires_route='image' 时,未绑凭据自动置灰+去绑定(加性,可选)。 */
  aiRoutes?: AiRoutesState
  /** AI 内容合规同意态:动作命令声明 requires_route(image/llm)时,未接受当前条款则拦首击弹条款(加性,可选)。 */
  aiConsent?: AiConsentState
  dispatch?: DispatchFn
}

export function GalleryEditor({
  editorClass = 'gallery',
  entityType, editorName, extraZones, afterZones, afterSize, afterGrow, typeLabel, entities, selectedId, onSelect, multiSelection, onSelectMulti, assetUrl,
  thumbField, scoreField, labelField, descriptionField, summaryFields, fieldSchemas, layoutMode,
  searchFields, requiredFilters, filters, sorts, defaultSort, actions, cardSize, visibilityField, lockedField, showThumb,
  thumbSizePreferenceKey, viewStateScope, statusOf, onAdopt, deleteCommand, duplicateCommand, reorderCommand, onImport, onAdd, emptyTextKey, acceptedMime, commandSpecs, aiRoutes, aiConsent, dispatch,
}: GalleryEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  // 命令返回导出件(op.external.kind==='export')→ 解析 asset_ref 触发浏览器下载。否则 ZIP 在服务端
  // 生成了却没人下载,用户「点了没反应」。空导出(count=0)由命令报错走下方 catch 显示。
  /** 命令产出是不是一份可下载的导出件 —— 是则回执就是那个下载动作本身,不再另给文字提示。 */
  const isExportOutcome = (res: unknown): boolean =>
    (res as { op?: { external?: { kind?: string } } } | undefined)?.op?.external?.kind === 'export'
  const downloadIfExport = (res: unknown): void => {
    const ext = (res as { op?: { external?: { kind?: string; artifact?: { asset_ref?: AssetRefDict; filename?: string } } } } | undefined)?.op?.external
    if (ext?.kind !== 'export' || !ext.artifact?.asset_ref) return
    const url = assetUrl?.(ext.artifact.asset_ref)
    if (url) triggerDownload(url, ext.artifact.filename || 'export.zip')
  }
  const typeName = typeLabel ?? entityType
  // 命令进行中是瞬时态;搜索/pill/排序/缩略图属个人视图态,在下方统一接 store。
  const [busyAction, setBusyAction] = useState<string | null>(null)
  // 样式化确认弹窗(替代裸 window.confirm)+ 批量操作错误回报(.ze-callout,与 pipeline 错误呈现一致)。
  const [confirmReq, setConfirmReq] = useState<{ text: string; run: () => void; variant?: 'primary' | 'danger' } | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  /** 动作成功后的即时回执。派发一个后台流水线(批量生成)只是「交办出去了」,命令立刻返回、
   *  busy 态随即消失 —— 界面上成功与失败长得一模一样,都是什么都不发生。一个要花几十秒和
   *  额度的动作,点下去必须当场有回音。(2026-07-26 空态 QA 实测挖出) */
  const [actionNotice, setActionNotice] = useState<string | null>(null)
  // withParams 动作(如「批量生成」):点击先开参数小窗(命令 params_schema 驱动),收集后 dispatch。
  const [paramAction, setParamAction] = useState<GalleryAction | null>(null)
  const [paramDraft, setParamDraft] = useState<Record<string, unknown>>({})
  const paramSpec = paramAction ? (commandSpecs ?? []).find((s) => s.id === paramAction.command) ?? null : null

  // 卡片尺寸:mod 预设语义档 → 初始连续 scale;用户滑杆覆盖为纯视图偏好。
  const defaultThumbSize = CARD_SIZE_TO_THUMB_SCALE[cardSize ?? 'balanced']
  const legacyThumbSize = viewStateScope
    ? defaultThumbSize
    : readThumbSizePreference(thumbSizePreferenceKey, defaultThumbSize)
  const defaultGalleryView = useMemo<GalleryViewState>(() => ({
    query: '', pills: {}, sortId: null, thumbSize: legacyThumbSize,
  }), [legacyThumbSize])
  const galleryCodec = useMemo(() => galleryViewStateCodec(defaultThumbSize), [defaultThumbSize])
  const galleryView = useScopedViewState({
    scope: viewStateScope,
    // 旧 key 不属于新用户 scope;仅在无 scope 兼容路径用 slice identity 防 key 切换泄漏。
    slice: viewStateScope
      ? GALLERY_VIEW_STATE_SLICE
      : `${GALLERY_VIEW_STATE_SLICE}:legacy:${thumbSizePreferenceKey ?? 'memory'}`,
    codec: galleryCodec,
    defaultValue: defaultGalleryView,
  })
  const { query, pills, sortId, thumbSize } = galleryView.value
  const setPersistentThumbSize = (next: number) => {
    const normalized = clampThumbScale(next, defaultThumbSize)
    galleryView.setValue((previous) => ({ ...previous, thumbSize: normalized }))
    if (!viewStateScope) writeThumbSizePreference(thumbSizePreferenceKey, normalized)
  }
  const effThumbSize = clampThumbScale(thumbSize, defaultThumbSize)

  const searchOn = searchFields !== undefined
  const effSearchFields = useMemo(
    () => resolveSearchFields(searchFields, labelField, descriptionField),
    [searchFields, labelField, descriptionField],
  )
  const activeFilters = useMemo<ActiveFilter[]>(() => {
    const out: ActiveFilter[] = [...(requiredFilters ?? [])]
    for (const f of filters ?? []) {
      const value = pills[f.id]
      if (value) out.push({ field: f.field, value: String(value) })
    }
    return out
  }, [requiredFilters, filters, pills])
  const activeSort = useMemo(() => resolveDefaultSort(sorts, sortId ?? defaultSort), [sorts, sortId, defaultSort])
  const activeSortId = activeSort?.id ?? ''
  // 可见集 = 搜索 + pill 收窄 + 视图排序;选择语义(⌘A/range/marquee)随可见集走
  const filtered = useMemo(
    () => filterCards(entities, { query: searchOn ? query : '', searchFields: effSearchFields, active: activeFilters }),
    [entities, searchOn, query, effSearchFields, activeFilters],
  )
  const visible = useMemo(
    () => sortCards(filtered, sorts, activeSortId || undefined),
    [filtered, sorts, activeSortId],
  )

  // 完整多选:⌘A 全选 / ⌘⇧A 反选 / Esc 清空(均对可见集)。
  const orderedIds = useMemo(() => visible.map((e) => e.id), [visible])
  const rawSelectionIds = useMemo(
    () => (multiSelection && multiSelection.length ? multiSelection : selectedId ? [selectedId] : []),
    [multiSelection, selectedId],
  )
  const selectionIds = useMemo(() => {
    const raw = new Set(rawSelectionIds)
    return orderedIds.filter((id) => raw.has(id))
  }, [orderedIds, rawSelectionIds])
  const selectedSet = useMemo(() => new Set(selectionIds), [selectionIds])
  const rootRef = useRef<HTMLDivElement>(null)
  const shortcuts = useMemo<ShortcutSpec[]>(() => {
    if (!onSelectMulti) return []
    const all = (e: KeyboardEvent) => { e.preventDefault(); onSelectMulti('', 'all', orderedIds) }
    const inv = (e: KeyboardEvent) => { e.preventDefault(); onSelectMulti('', 'invert', orderedIds) }
    return [
      { key: 'a', meta: true, handler: all },
      { key: 'a', ctrl: true, handler: all },
      { key: 'a', meta: true, shift: true, handler: inv },
      { key: 'a', ctrl: true, shift: true, handler: inv },
      { key: 'Escape', handler: () => onSelectMulti('', 'none', orderedIds) },
    ]
  }, [onSelectMulti, orderedIds])
  useEditorShortcuts(rootRef, shortcuts)

  const filteredOut = visible.length !== entities.length
  const count = selectedSet.size > 1
    ? `${selectedSet.size}/${visible.length}`
    : filteredOut ? `${visible.length}/${entities.length}` : entities.length

  // 动作区派发:selection 范围只带当前可见列表中真正选中的 ids;global 只带 type。
  const fireAction = (a: GalleryAction) => {
    if (a.scope === 'selection' && selectionIds.length === 0) {
      setActionError(t('editor.selectBeforeAction'))
      return
    }
    // withParams:先开参数小窗(下方渲染),收集后再 dispatch —— 不在此直发。
    if (a.withParams) {
      const spec = (commandSpecs ?? []).find((s) => s.id === a.command) ?? null
      setParamDraft(buildStartDraft(spec, {}, {}, null))
      setParamAction(a)
      return
    }
    const run = async () => {
      setActionError(null)
      setActionNotice(null)
      setBusyAction(a.command)
      try {
        if (a.scope === 'selection' && (a.command === 'entity.delete' || a.command === duplicateCommand)) {
          // 内置 entity.delete / 逐卡 duplicateCommand 都只收单实体 {type,id,expected_version}(批量
          // {type,ids} 会 KeyError 崩);逐 id 带 CAS 版本派发(进 OpLog 可全局撤),与 GridZone/TableZone 右键一致。
          const byId = new Map(entities.map((e) => [e.id, e]))
          for (const id of selectionIds) {
            const e = byId.get(id)
            if (e && !e.deleted) await Promise.resolve(fire?.(a.command, { type: entityType, id, expected_version: e.version }))
          }
        } else {
          // mod 批量命令(自定义,收 {type,ids})/ global 命令(只带 type)。
          const args = a.scope === 'selection' ? { type: entityType, ids: selectionIds } : { type: entityType }
          const outcome = await Promise.resolve(fire?.(a.command, args))
          downloadIfExport(outcome)
          // 后台流水线类动作(kind:'stage')没有可下载产物,回执就是它唯一的「我收到了」。
          if (!isExportOutcome(outcome)) {
            const n = a.scope === 'selection' ? selectionIds.length : visible.length
            setActionNotice(t('editor.actionStarted', { action: resolveActionLabel(a, t), n }))
          }
        }
      } catch (err) {
        // 首失败即停(await 抛出已退出循环)+ 报错(非静默续删);展示走 .ze-callout(同 pipeline)。
        setActionError(err instanceof Error ? err.message : String(err))
      } finally {
        setBusyAction(null)
      }
    }
    // confirm 守卫:样式化弹窗(删除用 danger 变体)替代裸 window.confirm。
    const confirmText = resolveActionConfirm(a, t, resolveActionLabel(a, t))
    if (confirmText !== undefined) {
      setConfirmReq({ text: confirmText, run: () => { void run() }, variant: a.command === 'entity.delete' ? 'danger' : 'primary' })
      return
    }
    void run()
  }

  // withParams 小窗提交:把收集到的参数 + type 作为命令 args 派发(如 objects.generate {count,theme,mode})。
  const submitParams = async () => {
    if (!paramAction) return
    const cmd = paramAction.command
    setActionError(null)
    setBusyAction(cmd)
    try {
      downloadIfExport(await Promise.resolve(fire?.(cmd, { type: entityType, ...paramDraft })))
      setParamAction(null)
    } catch (err) {
      // 空导出 / 命令失败:报错(.ze-callout),不静默吞(此前无 catch → 未处理 rejection,看着像没反应)
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyAction(null)
    }
  }

  // 控制功能区(control 行,wrap-cluster):① 选择簇(全选/已选 N/反选/清空,暴露既有 selectionModel 模式为可见控件)
  // ② 导入入口(mod 声明 importCommand 时)③ mod 动作全集常驻;scope:selection 无选中时只拦截行为,不改变 UI 结构。
  // 工具/管理/命令统一归 control 角色(功能区统一模型);视图筛选(pill/搜索)在 filterbar 不在此。
  const fileInputRef = useRef<HTMLInputElement>(null)
  // duplicateCommand 声明后注入一个内置「复制」选中动作(逐卡 fan-out,见 fireAction);
  // 与 mod 自声明 actions 并列,归 'other' 组(命令名不匹配 generate/disposition/export/danger)。
  const duplicateAction: GalleryAction | null = duplicateCommand
    ? { command: duplicateCommand, labelKey: 'editor.duplicate', scope: 'selection', variant: 'ghost' }
    : null
  const visibleActions = duplicateAction ? [...(actions ?? []), duplicateAction] : (actions ?? [])
  const actionGroups = orderedActionGroups(visibleActions)
  // 命令按钮缺省 secondary(静止可见轮廓;行>组>件:可点性靠件自身边界,不靠位置猜);mod 显式变体优先。
  const renderActionButton = (a: GalleryAction) => {
    const btn = (
      <button key={a.command} type="button" className="ze-button" data-size="sm"
        data-variant={a.variant ?? 'secondary'}
        data-ze-control={a.controlId || undefined}
        aria-label={resolveActionLabel(a, t)}
        disabled={busyAction !== null}
        onClick={() => { void fireAction(a) }}
      >{busyAction === a.command ? <span className="ze-spinner" data-size="sm" aria-hidden="true" /> : resolveActionLabel(a, t)}</button>
    )
    // 3.56/3.65 上生到 action:requires_route='image' 且未绑凭据 → AiRouteGate 置灰+去绑定;
    // 任何 requires_route 且未接受当前条款 → AiConsentGate 拦首击弹条款。条款门在外(先接受),
    // 路线门在内;未声明/无 state/条款未配时逐层透明透传(加性零影响)。key 落最外层元素。
    const spec = (commandSpecs ?? []).find((s) => s.id === a.command)
    let node = btn
    if (spec?.requires_route === 'image' && aiRoutes) {
      node = <AiRouteGate key={a.command} state={aiRoutes}>{node}</AiRouteGate>
    }
    if (spec?.requires_route && aiConsent) {
      node = <AiConsentGate key={a.command} state={aiConsent}>{node}</AiConsentGate>
    }
    return node
  }
  // 按需渲染:选择簇 UI 只在存在吃多选的动作(scope:'selection')时出现——没有消费者的
  // 全选/反选/清空是死控件;键盘多选能力(⌘A/⇧range)不随 UI 撤,仍走 onSelectMulti。
  // hasControls 与内部渲染共用同一判据,防"外门开着内里全空"的空 zone(死容器)挂载。
  const hasSelectionCluster = !!onSelectMulti && visibleActions.some((a) => a.scope === 'selection')
  const hasControls = hasSelectionCluster || !!onAdd || !!onImport || visibleActions.length > 0
  const controlZone = hasControls ? (
    <FunctionZone
      key="control"
      id="tool" role="control" kind="tool" layout="wrap-cluster"
    >
      {/* 选择簇/导入:命令件缺省 secondary(静止轮廓,可点性可见);组=独立 .ze-row 不可拆换行单元。 */}
      {hasSelectionCluster && onSelectMulti ? (
        <span className="ze-row" data-gap="1" role="group">
          <button type="button" className="ze-button" data-size="sm" data-variant="secondary" data-flex="0"
            onClick={() => onSelectMulti('', 'all', orderedIds)}>{t('editor.selectAll')}</button>
          <span className="ze-badge" data-size="sm" data-flex="0">{t('editor.selectedN', { n: selectionIds.length })}</span>
          <button type="button" className="ze-button" data-size="sm" data-variant="secondary" data-flex="0"
            onClick={() => onSelectMulti('', 'invert', orderedIds)}>{t('editor.invertSelection')}</button>
          <button type="button" className="ze-button" data-size="sm" data-variant="secondary" data-flex="0"
            onClick={() => onSelectMulti('', 'none', orderedIds)}>{t('editor.clearSelection')}</button>
        </span>
      ) : null}
      {onAdd || onImport ? (
        <span className="ze-row" data-gap="1" role="group">
          {onAdd ? (
            <button type="button" className="ze-button" data-size="sm" data-variant="secondary"
              data-ze-gallery-add="" onClick={onAdd}>{t('editor.add')}</button>
          ) : null}
          {onImport ? (
            <button type="button" className="ze-button" data-size="sm" data-variant="secondary"
              onClick={() => fileInputRef.current?.click()}>{t('editor.import')}</button>
          ) : null}
          {onImport ? (
            <input ref={fileInputRef} type="file" accept={acceptedMime ?? 'image/*'} hidden
              onChange={(e) => { const f = e.target.files?.[0]; if (f) void onImport(f); e.target.value = '' }} />
          ) : null}
        </span>
      ) : null}
      {actionGroups.map((group) => (
        <span key={galleryActionGroup(group[0])} className="ze-row" data-gap="1" role="group">
          {group.map(renderActionButton)}
        </span>
      ))}
    </FunctionZone>
  ) : null

  // 三功能区:filterbar(搜索+pill 筛选+缩略图尺寸)+ control(选择簇/导入/命令)。内容区 = GridZone。
  const zones: ReactNode[] = []
  zones.push(
    <FilterBarZone
      key="filterbar"
      filters={filters ?? []} entities={entities} pills={pills}
      onPillChange={(id, value) => galleryView.setValue((previous) => ({
        ...previous, pills: { ...previous.pills, [id]: value },
      }))}
      search={searchOn ? {
        query,
        onQueryChange: (nextQuery) => galleryView.setValue((previous) => ({ ...previous, query: nextQuery })),
      } : undefined}
      sort={sorts?.length ? {
        value: activeSortId,
        options: sorts,
        onChange: (nextSortId) => galleryView.setValue((previous) => ({ ...previous, sortId: nextSortId })),
      } : undefined}
      // 按需渲染:无媒体列表(showThumb=false 纯文本卡)没有缩略图可调,不渲尺寸滑杆(死控件)。
      thumbSize={showThumb === false ? undefined : {
        value: effThumbSize,
        min: THUMB_SCALE_MIN,
        max: THUMB_SCALE_MAX,
        step: 'any',
        onChange: setPersistentThumbSize,
      }}
    />,
  )
  if (controlZone) zones.push(controlZone)
  if (extraZones?.length) zones.push(...extraZones)   // 注入功能区(content 之前):PromptZone / custom zone

  return (
    <>
    <EditorFrame
      editorClass={editorClass} rootRef={rootRef}
      header={<HeaderZone title={editorName ?? typeName} count={count} />}
      zones={zones.length ? zones : undefined}
      afterZones={afterZones} afterSize={afterSize} afterGrow={afterGrow}
      // 卡片流自带边界(卡=边框+底色) → content 直通,不再画 sunken 壳(防 content→card 冗余壳链)
      contentMode="pass-through"
    >
      {actionError ? (
        <div className="ze-callout" data-variant="danger" role="alert">
          <span className="ze-callout__body">{actionError}</span>
        </div>
      ) : null}
      {actionNotice && !actionError ? (
        <div className="ze-callout" data-variant="info" role="status" data-ze-action-notice="">
          <span className="ze-callout__body">{actionNotice}</span>
        </div>
      ) : null}
      <GridZone
        entityType={entityType} entities={visible} thumbField={thumbField}
        scoreField={scoreField} labelField={labelField} descriptionField={descriptionField}
        summaryFields={summaryFields} fieldSchemas={fieldSchemas} layoutMode={showThumb === false ? (layoutMode ?? 'card-list') : layoutMode}
        thumbScale={effThumbSize} showThumb={showThumb}
        visibilityField={visibilityField} lockedField={lockedField}
        selectedId={selectedId}
        multiSelection={multiSelection} onSelect={onSelect} onSelectMulti={onSelectMulti}
        assetUrl={assetUrl} statusOf={statusOf} onAdopt={onAdopt} deleteCommand={deleteCommand} reorderCommand={reorderCommand}
        // mod 的空态文案是给「一条都没有」写的(它要指路:怎么才能有第一条)。筛选把可见集筛空
        // 时条目其实是有的,再说「还没有」就是误导 —— 那种情况回落框架的中性文案。
        dispatch={fire} emptyText={emptyTextKey && entities.length === 0
          ? t(emptyTextKey) : t('editor.empty', { type: typeName })}
      />
    </EditorFrame>
    {paramAction && (
      <BaseDialog
        title={resolveActionLabel(paramAction, t)}
        size="sm"
        onClose={() => setParamAction(null)}
        footer={
          <>
            <button className="ze-button" data-size="sm" onClick={() => setParamAction(null)}>{t('common.cancel')}</button>
            <button
              className="ze-button" data-variant="primary" data-size="sm"
              disabled={busyAction !== null || missingRequiredParams(paramSpec, paramDraft).length > 0}
              onClick={() => { void submitParams() }}
            >
              {busyAction === paramAction.command
                ? <span className="ze-spinner" data-size="sm" aria-hidden="true" />
                : resolveActionLabel(paramAction, t)}
            </button>
          </>
        }
      >
        <ParamPanel
          schema={{ type: 'object', properties: paramProperties(paramSpec) } as EntityTypeSchema}
          values={paramDraft}
          onCommit={(f, v) => setParamDraft((d) => ({ ...d, [f]: v }))}
        />
      </BaseDialog>
    )}
    {confirmReq && (
      <ConfirmDialog
        message={confirmReq.text}
        variant={confirmReq.variant}
        onConfirm={() => { confirmReq.run(); setConfirmReq(null) }}
        onCancel={() => setConfirmReq(null)}
      />
    )}
    </>
  )
}
