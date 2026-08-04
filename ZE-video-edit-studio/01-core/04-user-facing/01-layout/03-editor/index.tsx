/**
 * 03-editor — 对外入口
 *
 * 协议: ./SPEC.md + ./EDITOR-PHILOSOPHY.md。
 * 视觉: 04-editors 一编辑器类型一皮肤;代码: editors/<id>/ 一编辑器一子模块 + 03-zones 的 zone 组合。
 *
 * editor class 注册表(开集):table / form 自 M1;gallery / task / canvas2d / scene3d /
 * asset / media 本轮落地(Block2);timeline(1D 时序)/ text(长文)/ code(代码)/
 * graph(节点图)/ pipeline(管线)/ review(审阅)/ compare(对比)全部落地 —— 16 类编辑器全实装,无 null 占位。
 * 装配怎么把 SlotContext 接到各 editor 的 props 见 ../lib/slotAdapters.tsx。
 */

// 编辑器基座(容器 / toolbar 槽 / statusbar / 空态 / 加载壳 + 空间家族共享 body 脚手架)
import '@ze/css/04-editors/shell/editor/code.css'
// 各编辑器类型内容画法(一类型一皮肤);canvas2d & asset 的空间基元走共享 spatial.css
import '@ze/css/04-editors/data/table/code.css'
import '@ze/css/04-editors/input/form/code.css'
import '@ze/css/04-editors/gallery/gallery/code.css'
import '@ze/css/04-editors/workflow/task/code.css'
import '@ze/css/04-editors/shell/spatial/code.css'
import '@ze/css/04-editors/media/compare/code.css'
import '@ze/css/04-editors/media/scene3d/code.css'
import '@ze/css/04-editors/media/asset/code.css'
import '@ze/css/04-editors/media/media/code.css'
import '@ze/css/04-editors/data/chart/code.css'
import '@ze/css/04-editors/workflow/timeline/code.css'
import '@ze/css/04-editors/workflow/keyframe-curve/code.css'
import '@ze/css/04-editors/input/text/code.css'
import '@ze/css/04-editors/input/code/code.css'
import '@ze/css/04-editors/input/html/code.css'
import '@ze/css/04-editors/input/document/code.css'
import '@ze/css/04-editors/workflow/dialog/code.css'
import '@ze/css/04-editors/workflow/graph/code.css'
import '@ze/css/04-editors/workflow/review/code.css'
import '@ze/css/04-editors/workflow/pipeline/code.css'
// editor 周边 UI 组合(功能区 zone):toolbar 已去令牌删除(纯组合 L2);inspector 留结构 css
import '@ze/css/03-zones/editor/inspector/code.css'
import '@ze/css/03-zones/navigation/outliner/code.css'  // outline 编辑器树节点视觉

import type { ComponentType } from 'react'
import { TableEditor } from './editors/table/TableEditor'
import { FormEditor } from './editors/form/FormEditor'
import { GalleryEditor } from './editors/gallery/GalleryEditor'
import { TaskEditor } from './editors/task/TaskEditor'
import { Canvas2dEditor } from './editors/canvas2d/Canvas2dEditor'
import { Scene3dEditor } from './editors/scene3d/Scene3dEditor'
import { AssetEditor } from './editors/asset/AssetEditor'
import { MediaEditor } from './editors/media/MediaEditor'
import { ChartEditor } from './editors/chart/ChartEditor'
import { TimelineEditor } from './editors/timeline/TimelineEditor'
import { CurveEditor } from './editors/keyframe-curve/CurveEditor'
import { TextEditor } from './editors/text/TextEditor'
import { CodeEditor } from './editors/code/CodeEditor'
import { GraphEditor } from './editors/graph/GraphEditor'
import { ReviewEditor } from './editors/review/ReviewEditor'
import { CompareEditor } from './editors/compare/CompareEditor'
import { PipelineEditor } from './editors/pipeline/PipelineEditor'
import { OutlineEditor } from './editors/outline/OutlineEditor'
import { HtmlEditor } from './editors/html/HtmlEditor'
import { DialogEditor } from './editors/dialog/DialogEditor'
import { DocumentEditor } from './editors/document/DocumentEditor'
import type { EditorClass } from './types'

export { TableEditor, type TableEditorProps } from './editors/table/TableEditor'
export { FormEditor, type FormEditorProps } from './editors/form/FormEditor'
export { GalleryEditor, type GalleryEditorProps } from './editors/gallery/GalleryEditor'
export { FormEditor as PropertyEditor, type FormEditorProps as PropertyEditorProps } from './editors/form/FormEditor'
export { GalleryEditor as CardListEditor, type GalleryEditorProps as CardListEditorProps } from './editors/gallery/GalleryEditor'
export { TaskEditor, type TaskEditorProps } from './editors/task/TaskEditor'
export { Canvas2dEditor, type Canvas2dEditorProps } from './editors/canvas2d/Canvas2dEditor'
export {
  Scene3dEditor,
  type Scene3dEditorProps, type Scene3dRenderer, type Scene3dRendererProps,
  type Scene3dCameraMode, type Scene3dCameraChangeMeta, type Scene3dControls,
} from './editors/scene3d/Scene3dEditor'
export {
  asScene3dOrbitCamera, normalizeScene3dCameraSnapshot,
  type Scene3dCameraSnapshot, type Scene3dOrbitCamera,
} from './lib/scene3dViewState'
export { AssetEditor, type AssetEditorProps } from './editors/asset/AssetEditor'
export { MediaEditor, type MediaEditorProps } from './editors/media/MediaEditor'
export { ChartEditor, type ChartEditorProps, type ChartKind } from './editors/chart/ChartEditor'
export { TimelineEditor, type TimelineEditorProps, type TimelineFields } from './editors/timeline/TimelineEditor'
export { CurveEditor, type CurveEditorProps, type CurveFields } from './editors/keyframe-curve/CurveEditor'
export { TextEditor, type TextEditorProps } from './editors/text/TextEditor'
export { CodeEditor, type CodeEditorProps } from './editors/code/CodeEditor'
export { GraphEditor, type GraphEditorProps, type GraphFields } from './editors/graph/GraphEditor'
export { ReviewEditor, type ReviewEditorProps } from './editors/review/ReviewEditor'
export { CompareEditor, type CompareEditorProps } from './editors/compare/CompareEditor'
export { CompareZone, type CompareZoneProps } from './zones/CompareZone'
export { PipelineEditor, type PipelineEditorProps } from './editors/pipeline/PipelineEditor'
export { OutlineEditor, type OutlineEditorProps } from './editors/outline/OutlineEditor'
export { HtmlEditor, type HtmlEditorProps } from './editors/html/HtmlEditor'
export { DialogEditor, type DialogEditorProps } from './editors/dialog/DialogEditor'
export { DialogEditor as ConversationEditor, type DialogEditorProps as ConversationEditorProps } from './editors/dialog/DialogEditor'
export { DocumentEditor, type DocumentEditorProps } from './editors/document/DocumentEditor'
export { renderBlockHtml, blockTypeOf, blockIsTextEditable, type BlockFieldMap } from './editors/document/blockRegistry'
export { OutlinerZone, type OutlinerZoneProps } from './zones/OutlinerZone'
export {
  childrenByParent, flattenVisible, isDescendant, computeReparent,
  DEFAULT_TREE_FIELDS, type TreeFieldMap, type TreeRow, type ReparentUpdate,
} from './lib/treeModel'
export { EditorDispatchContext, useEditorDispatch, type DispatchFn } from './lib/dispatch'
// 通用懒模块注册表(自动下载机制):重渲染器/重库按需拉取,core 不打包
export {
  registerLazyModule, unregisterLazyModule, isLazyModuleRegistered, useLazyModule,
  type ModuleLoader, type UseLazyModule,
} from './lib/lazyModule'
export {
  SkeletonText, SkeletonBlock, SkeletonCircle, SkeletonLines,
  ProgressRing, ErrorPlaceholder,
} from './lib/Skeleton'
// 统一、尺寸稳定的加载/处理态外壳(WS-I):占位 = 加载后 footprint,零 reflow + AI busy 同尺寸叠加
export { EntityLoadingFrame, type EntityLoadingFrameProps } from './lib/EntityLoadingFrame'
// canvas 系(asset/media/scene3d/canvas2d)统一"处理中"覆盖层:内容形骨架 + 进度环,加载前后零 reflow
export { CanvasBusyOverlay } from './lib/CanvasBusyOverlay'
// 共享子系统(P0):右键菜单 / 局部快捷键 / 局部状态条
export { ContextMenu, type ContextMenuItem, type ContextMenuProps } from './primitives/ContextMenu'
export { useContextMenu, type ContextMenuState, type UseContextMenu } from './primitives/useContextMenu'
export { EditorStatusBar, type EditorStatusBarProps } from './primitives/EditorStatusBar'
export { useEditorShortcuts, type ShortcutSpec } from './lib/useEditorShortcuts'
// 功能区(zone)组件层 + 拓展点契约(协议 03-css-rules/03-zones/SPEC.md)
export { EditorFrame, type EditorFrameProps } from './EditorFrame'
export { ToolbarZone } from './zones/ToolbarZone'
export { StatusZone } from './zones/StatusZone'
export { FilterBarZone as ManageZone, type FilterBarZoneProps as ManageZoneProps } from './zones/FilterBarZone'
export { ControlZone as ToolZone, type ControlZoneProps as ToolZoneProps } from './zones/ControlZone'
export { PromptZone, type PromptZoneProps } from './zones/PromptZone'
export { FunctionZone, type FunctionZoneProps } from './zones/FunctionZone'
export { HeaderZone, type HeaderZoneProps } from './zones/HeaderZone'
export { TableZone, type TableZoneProps } from './zones/TableZone'
export { GridZone, type GridZoneProps } from './zones/GridZone'
export { CanvasToolbarZone, type CanvasToolbarZoneProps } from './zones/CanvasToolbarZone'
export { ActionBarZone, type ActionBarZoneProps } from './zones/ActionBarZone'
export { FieldRow, type FieldRowProps } from './zones/FieldRow'
export { InfoControlUnit, type InfoControlUnitProps } from './zones/InfoControlUnit'
export type {
  ZoneId, ActionZoneId, ContentZoneId, ZoneRegion,
  ToolbarAction, StatusSegment, ListColumn, RowAction, GridFilter, CardSort, GalleryAction, FormAction, InspectorSectionSpec,
  ToolbarZoneProps, StatusZoneProps, ZoneOverrides,
} from './zones/types'
// 属性面板纯逻辑(动作置灰/scope 派发/聚合编辑判定/单元解析/共享钮渲染;ActionBarZone/InfoControlUnit/FormZone + 装配缝消费)
export { missingFields, actionArgs, actionVisible, parseFormActions, parseInspectorSections, renderFormActionButton, isEmptyValue } from './zones/actionGating'
export { aggregateFields, aggregateFieldList, batchUpdateArgs, type AggregatedField } from './zones/aggregateFields'
// 卡片列表编辑器纯逻辑(过滤/尺寸 preset/view_state 解析守卫;装配缝 slotAdapters 消费)
export {
  THUMB_SIZES, asThumbSize, filterCards, filterValues, resolveSearchFields, sortCards, resolveDefaultSort, readCardField,
  parseGridFilters, parseCardSorts, parseGalleryActions, parseRequiredCardFilters, type ThumbSize, type ActiveFilter, type SortValueType,
  thumbSizeAtIndex, thumbSizeIndex, thumbScaleForSize, clampThumbScale, thumbGridMinWidthForScale,
  thumbListMediaWidthForScale, thumbCompactMediaWidthForScale,
  THUMB_SCALE_MIN, THUMB_SCALE_MAX, CARD_SIZES, asCardSize, CARD_SIZE_TO_THUMB, CARD_SIZE_TO_THUMB_SCALE, type CardSize,
} from './lib/cardListModel'
// 空间编辑器(P1):2D 交互式画布(视口/数学/gizmo/指针状态机)
export { SpatialCanvas, type SpatialBox, type SpatialCanvasProps } from './primitives/SpatialCanvas'
export { TransformGizmo, type TransformGizmoProps } from './primitives/TransformGizmo'
export { Canvas2dSpatial, readGeometry, writeGeometryPatch } from './primitives/Canvas2dSpatial'
// 1.27.0:Canvas2dLayerEditor 纯化更名 Canvas2dLayerStage(图层列表/属性面板摘除——
// 那是卡片列表编辑器 + form 的职责,经 L5 layered_canvas 组合;SPEC §2.1b 三律)
export { Canvas2dLayerStage, type Canvas2dLayerStageProps } from './primitives/Canvas2dLayerStage'
export { useViewport, type UseViewport } from './primitives/useViewport'
export { useSpatialPointer, type UseSpatialPointer, type PointerKind } from './primitives/useSpatialPointer'
export * as spatialMath from './primitives/spatialMath'
export {
  DEFAULT_CONSTRAINTS,
  type Box, type Geometry, type Vec2, type Viewport,
  type GeometryFieldMap, type GizmoConstraints, type HandleId,
} from './primitives/spatialTypes'
// primitives(框架级,editor + mod 可复用)
export { SectionTitle, Collapsible, InfoField, type CollapsibleProps } from './primitives/InspectorSection'
export { Slider, type SliderProps } from './primitives/Slider'
export { ParamPanel, type ParamPanelProps } from './primitives/ParamPanel'
export { AiCandidateDialog, type AiCandidateDialogProps } from './primitives/AiCandidateDialog'
export { ThumbnailGrid, ThumbnailCard, type ThumbItem, type ThumbnailGridProps, type ThumbnailLayoutMode } from './primitives/ThumbnailGrid'
export { ImageLightbox, type ImageLightboxProps } from './primitives/ImageLightbox'
export { CanvasViewport, type CanvasViewportProps } from './primitives/CanvasViewport'
export { CanvasToolbar, type CanvasToolbarProps } from './primitives/CanvasToolbar'
export { CANVAS_TOOLS, type ToolId, type ToolDef, type StrokeRecord, type StrokePoint } from './primitives/canvasTools'
export {
  resolveCanvas2dPreset, isCanvas2dPreset, isCanvas2dAssetRef,
  createCanvas2dLayer, normalizeCanvas2dLayer, readCanvas2dLayers, canvas2dLayerCanEdit,
  addCanvas2dLayer, patchCanvas2dLayer, canvas2dLayerFilterPatch, canvas2dLayerFilterCss, canvas2dLayerStyle,
  normalizeCanvas2dFilter, normalizeCanvas2dFilterStack, removeCanvas2dLayer, reorderCanvas2dLayer,
  layerEntityToCanvas2dLayer, readLayerEntities, expandCanvas2dDocument, shouldMarkRangeStale,
  type Canvas2dPreset, type Canvas2dLayerKind, type Canvas2dBlendMode, type Canvas2dAssetRef,
  type Canvas2dLayerFilter, type Canvas2dLayerStyle,
  type Canvas2dLayer, type Canvas2dDocument, type Canvas2dFeatureFlags, type Canvas2dViewState,
  type Canvas2dFieldMapping,
} from './primitives/canvas2dModel'
// 光栅 mask 图元(W1a):笔画累积成 mask 位图 + seed-from-asset + onMaskCommit(算法留 mod)
export { MaskCanvas, type MaskCanvasProps } from './primitives/MaskCanvas'
export {
  createMask, applyStroke, countFilled, isEmpty, alphaToRGBA, rgbaToAlpha,
  type MaskBuffer, type MaskMode, type MaskPoint, type StrokeOpts,
} from './primitives/maskRaster'
// A/B 前后对比图元(W4):并排 / 滑块 / 叠加
export { CompareFrame, type CompareFrameProps } from './primitives/CompareFrame'
export {
  COMPARE_MODES, isCompareMode, clampUnit, afterClipInset, pointerToSplit, type CompareMode,
} from './primitives/compareGeometry'
export type {
  EntityDict, FieldSchema, EntityTypeSchema, Column, EditorClass, Breakpoint,
  EntityRefOption, EntityRefOptions,
  FormActionResultSpec, FormActionResultConsumer,
  RunPhase, RunError, EntityRunStatus, StageStatus, StatusLookup, LoadingStatus,
  AssetRefDict, AssetUrlFn, CustomPaneProps,
} from './types'

/**
 * editor class 注册表(开集; SPEC §2.2)。16 类全实装(无 null 占位)。
 */
export const EDITOR_CLASSES: Record<EditorClass, ComponentType<any> | null> = {
  table: TableEditor,
  form: FormEditor,
  property: FormEditor,
  gallery: GalleryEditor,
  cardlist: GalleryEditor,
  task: TaskEditor,
  canvas2d: Canvas2dEditor,
  scene3d: Scene3dEditor,
  asset: AssetEditor,
  media: MediaEditor,
  chart: ChartEditor,
  timeline: TimelineEditor,
  'keyframe-curve': CurveEditor,
  text: TextEditor,
  code: CodeEditor,
  graph: GraphEditor,
  review: ReviewEditor,
  compare: CompareEditor,
  pipeline: PipelineEditor,
  outline: OutlineEditor,
  html: HtmlEditor,
  dialog: DialogEditor,
  conversation: DialogEditor,
  document: DocumentEditor,
  // 注入式自定义面板:core 无实装(null),由 mod 经 ctx.renderers[view_state.rendererKey] 提供。
  custom: null,
}
