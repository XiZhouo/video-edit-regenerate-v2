/**
 * 01-layout — 父级 layout 叶子对外入口(LayoutTree 中区分屏)
 *
 * 协议: ./SPEC.md §2.2。视觉: 03-css-rules/04-editors/layout.css(本文件触发加载)。
 *
 * 用法(装配层 / webapp):
 *   import { LayoutRenderer, SlotContext, usePersistedLayout, type LayoutNode }
 *     from '@ze/core/04-user-facing/01-layout'
 *   const [tree, setTree] = usePersistedLayout(SKILL_ID, meta?.id, DEMO_TREE)
 *   <SlotContext.Provider value={{...}}>
 *     <LayoutRenderer tree={tree} onChange={setTree} />
 *   </SlotContext.Provider>
 *
 * Header / Footer 是 Main 区的同级(不进 LayoutTree),各自从 01-header / 02-footer 引。
 */

import '@ze/css/05-compositions/layout/layout/code.css'

import type { LayoutNode } from './types'

export { LayoutRenderer } from './LayoutRenderer'
export { SlotContext, useSlotContext, type SlotContextValue } from './lib/SlotContext'
// custom pane 跨类型选择读写唯一入口(F8;防 props.selection / props.onSelectEntity footgun)
// F2:同一入口扩展跨面板高亮通道(highlights / setHighlights / clearHighlights,Rendering Hints 层)
export { useEditorSelection, editorSelectionOf, type EditorSelectionApi, type EditorHighlightChannel } from './lib/useEditorSelection'
export { applySelect, selectionModeFromEvent, type SelectionMode, type MultiSelection, type SelectMultiFn } from './lib/selectionModel'
export { usePersistedLayout } from './lib/usePersistedLayout'
export { updateNode, setRatio, setActiveTab, findNode, moveTab, collectNodeIds, sameNodeIdSet } from './lib/layoutOps'
export { renderSlot } from './lib/slotAdapters'
// editor-combination 层:布局预设 + mod 布局声明加载器
export {
  linearProcess, catalogBrowse, singleCanvas, pipelineBatch,
  PRESETS, type PresetName, type SlotSpec,
} from './lib/presets'
export { loadLayout, type ModLayoutConfig } from './lib/loadLayout'
export {
  createPersistenceRuntime,
  persistenceFromMeta,
  persistenceFromReceipt,
  reducePersistence,
  type PersistenceDocumentMeta,
  type PersistenceEvent,
  type PersistenceFacts,
  type PersistenceReceipt,
  type PersistenceRuntime,
  type PersistenceState,
  type SavePolicy,
  type CommitStatus,
  type EventStatus,
} from './persistenceState'
export {
  MIN_SLOT_W, MIN_SLOT_H, isSlot, isSplit, isTabs,
  type LayoutNode, type EditorSlotNode, type SplitNode, type TabsNode,
  type NodeId, type EntityBinding, type EditorClass,
} from './types'

/** core 兜底布局:单个 EditorSlot(无绑定)。mod 不声明 tree 时用。 */
export function coreDefaultTree(): LayoutNode {
  return { kind: 'slot', id: 'root', editor_class: 'table', entity_bindings: [] }
}
