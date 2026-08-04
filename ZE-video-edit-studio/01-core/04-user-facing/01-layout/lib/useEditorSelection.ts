/**
 * useEditorSelection — custom renderer 的跨类型选择读写唯一入口(typed helper,F8)。
 *
 * 背景(两次真实回归,见 audit-v3):CustomPaneProps 只透传宿主类型的 selectedId/onSelect;
 * 跨类型选择曾靠已删除的 props.selection(读全表)与 props.onSelectEntity(写别的类型),
 * core sync 后二者运行时 undefined → "reading 'rule'" / "onSelectEntity is not a function" 崩溃。
 * 本 hook 把「按 entity 类型读当前选中 + 跨类型写选中」收束成一个稳定契约:内部走公开的
 * SlotContext(与 layered 实体接缝同通道),provider 值变化即重渲染,custom pane 内订阅安全。
 *
 * 纯加性:不改 CustomPaneProps;单类型面板继续用 props.selectedId/props.onSelect 零迁移。
 * 读语义 = 严格指针解析(selection[type] 命中才返回实体,无 slotAdapters 的单实体回退)。
 *
 * F2 扩展(保持单一入口哲学):同一 hook 增加跨面板高亮通道读写(highlights /
 * highlightedIdsOf / setHighlights / clearHighlights)。高亮属 Rendering Hints 层——
 * 纯视觉会话态,不进 Document / OpLog / undo / 持久化;与 selection 互不干扰、可叠加。
 */

import { useMemo } from 'react'

import type { EntityDict } from '../03-editor'
import { useSlotContext } from './SlotContext'

export type EditorSelectionApi = {
  /** 跨类型 active 指针全表(selection[type] = id | null;等价回归前 props.selection)。 */
  selection: Record<string, string | null>
  /** 读:某 entity_type 当前选中 id(未选 → null)。 */
  selectedIdOf: (entityType: string) => string | null
  /** 读:某 entity_type 当前选中实体(未选 / id 不在实体表 → null)。 */
  selectedOf: (entityType: string) => EntityDict | null
  /** 写:选中某 entity_type 的某条(null = 清空);签名与回归前 props.onSelectEntity 一致。 */
  select: (entityType: string, id: string | null) => void
  /** 读:跨类型高亮全表(F2,Rendering Hints 层;纯视觉会话态,不进 Document / undo)。 */
  highlights: Record<string, string[]>
  /** 读:某 entity_type 当前高亮 id 集(无 → [])。 */
  highlightedIdsOf: (entityType: string) => string[]
  /** 写:设某 entity_type 的高亮集([] 等价清除);与 select 互不干扰、可叠加。 */
  setHighlights: (entityType: string, ids: string[]) => void
  /** 写:清某 entity_type 的高亮;其余类型不动。 */
  clearHighlights: (entityType: string) => void
}

/** 高亮通道注入(F2,可选加性参;装配层未接 = 读空表、写 no-op,seam 闭合不崩)。 */
export type EditorHighlightChannel = {
  highlights?: Record<string, string[]>
  setHighlights?: (entityType: string, ids: string[]) => void
  clearHighlights?: (entityType: string) => void
}

const NOOP = () => {}

/** 纯工厂(node 环境可测):由 selection 全表 + 实体表 + 写回调(+ 可选高亮通道)组装 API。 */
export function editorSelectionOf(
  selection: Record<string, string | null>,
  entitiesByType: Record<string, EntityDict[]>,
  onSelect: (entityType: string, id: string | null) => void,
  highlightChannel: EditorHighlightChannel = {},
): EditorSelectionApi {
  const highlights = highlightChannel.highlights ?? {}
  return {
    selection,
    selectedIdOf: (entityType) => selection[entityType] ?? null,
    selectedOf: (entityType) => {
      const id = selection[entityType] ?? null
      return id ? (entitiesByType[entityType] ?? []).find((e) => e.id === id) ?? null : null
    },
    select: onSelect,
    highlights,
    highlightedIdsOf: (entityType) => highlights[entityType] ?? [],
    setHighlights: highlightChannel.setHighlights ?? NOOP,
    clearHighlights: highlightChannel.clearHighlights ?? NOOP,
  }
}

export function useEditorSelection(): EditorSelectionApi {
  const { selection, entitiesByType, onSelect, highlights, setHighlights, clearHighlights } = useSlotContext()
  return useMemo(
    () => editorSelectionOf(selection, entitiesByType, onSelect, { highlights, setHighlights, clearHighlights }),
    [selection, entitiesByType, onSelect, highlights, setHighlights, clearHighlights],
  )
}
