/**
 * useInteractionStore — 会话级交互态(选择)具名化(L7 横向叶子)
 *
 * 把原本散在装配层(webapp/App)的"当前选择"会话态收成一个具名 hook:
 *   - selection      : 每个 entity_type 的 primary/active 指针(form/asset/canvas 读它)
 *   - multiSelection : 每个 entity_type 的多选集(table/gallery/task 用,单选编辑器忽略)
 *   - currentTime    : 共享时间游标/playhead(秒)= 与 selection 同级的第二条跨编辑器联动轴
 *                      (timeline/preview/keyframe/transport/media 都读写它;C0 地基)
 *   - highlights     : 跨面板高亮通道(F2)= Rendering Hints 层——A 面板选中某实体 →
 *                      B 面板(canvas2d 等)高亮一组相关对象。纯视觉会话态,与 selection 互不干扰
 *   - onSelect       : 设 active 指针(null=清空)
 *   - onSelectMulti  : 多选 reducer(replace/toggle/range…),active 指针跟随最后点击
 *   - setCurrentTime : 设共享时间游标(scrub/播放/seek 都经它,跨面板联动)
 *   - setHighlights / clearHighlights : 设/清某 entity_type 的高亮集(按类型隔离)
 *
 * 设计:沿用 02-interaction SPEC「横向叶子,谁都引用」。**纯会话态、不进 Document / OpLog**
 * (选择不是文档数据,不参与协作同步)。把 App 里的 useState/useRef + 处理器**原样上提**,
 * 行为字节一致 —— 选择语义、active 跟随、anchor 计算全不变,只换了归属(具名 hook ⟶ 可单测/可复用)。
 *
 * 跨编辑器同类型选择天然同步:两个 slot 绑同一 entity_type → 共享同一 selection[type]。
 */

import { useCallback, useRef, useState } from 'react'

import { applySelect, type SelectionMode } from '../01-layout/lib/selectionModel'

/** 跨类型「最近一次选中」指针(inspector selection-follow 读它);纯会话态 */
export type LastSelected = { entityType: string; id: string } | null

export type InteractionStore = {
  /** 每个 entity_type 当前 active 的 id(null=无)= primary 指针 */
  selection: Record<string, string | null>
  /** 每个 entity_type 的多选集(opt-in) */
  multiSelection: Record<string, string[]>
  /** 跨类型最近选中(form followSelection 用);清空匹配类型时归 null */
  lastSelected: LastSelected
  /** 共享时间游标/playhead(秒);与 selection 同级的跨编辑器联动轴(C0)。纯会话态、不进 Document/OpLog。 */
  currentTime: number
  /** 跨面板高亮通道(F2):每个 entity_type 当前高亮的 id 集。Rendering Hints 层——
   *  纯视觉会话态,不进 Document / OpLog / undo / 持久化;与 selection 互不干扰、可叠加。 */
  highlights: Record<string, string[]>
  /** 设 active 指针 */
  onSelect: (entityType: string, id: string | null) => void
  /** 多选变更;active 指针同步跟随最后点击的 id */
  onSelectMulti: (
    entityType: string,
    id: string,
    mode: SelectionMode,
    orderedIds: string[],
  ) => void
  /** 设共享时间游标(秒);scrub/播放/seek 都经它,任意编辑器写、绑同轴的编辑器读。 */
  setCurrentTime: (t: number) => void
  /** 设某 entity_type 的高亮集([] 等价清除);按类型隔离,不触碰 selection。 */
  setHighlights: (entityType: string, ids: string[]) => void
  /** 清某 entity_type 的高亮;其余类型的高亮不动。 */
  clearHighlights: (entityType: string) => void
  /** Project namespace changed: purge every ephemeral cross-editor pointer. */
  reset: () => void
}

/** 纯 reducer(node 可测):设某类型高亮集。存副本(防外部 mutate);空数组 = 删键(等价清除)。 */
export function setHighlightsIn(
  cur: Record<string, string[]>, entityType: string, ids: string[],
): Record<string, string[]> {
  if (ids.length === 0) return clearHighlightsIn(cur, entityType)
  return { ...cur, [entityType]: [...ids] }
}

/** 纯 reducer(node 可测):清某类型高亮;键不存在时返回原引用(不触发多余重渲染)。 */
export function clearHighlightsIn(
  cur: Record<string, string[]>, entityType: string,
): Record<string, string[]> {
  if (!(entityType in cur)) return cur
  const { [entityType]: _gone, ...rest } = cur
  return rest
}

export function useInteractionStore(): InteractionStore {
  const [selection, setSelection] = useState<Record<string, string | null>>({})
  const [multiSel, setMultiSel] = useState<Record<string, string[]>>({})
  const [lastSelected, setLastSelected] = useState<LastSelected>(null)
  const [currentTime, setCurrentTime] = useState<number>(0)
  const [highlights, setHighlightsState] = useState<Record<string, string[]>>({})
  const anchorRef = useRef<Record<string, string | null>>({})

  // F2 高亮通道:独立 useState,写路径不触碰 selection/multiSelection(两通道互不干扰、可叠加)
  const setHighlights = useCallback((entityType: string, ids: string[]) => {
    setHighlightsState((h) => setHighlightsIn(h, entityType, ids))
  }, [])
  const clearHighlights = useCallback((entityType: string) => {
    setHighlightsState((h) => clearHighlightsIn(h, entityType))
  }, [])

  const reset = useCallback(() => {
    setSelection({})
    setMultiSel({})
    setLastSelected(null)
    setCurrentTime(0)
    setHighlightsState({})
    anchorRef.current = {}
  }, [])

  const onSelect = useCallback((entityType: string, id: string | null) => {
    setSelection((s) => ({ ...s, [entityType]: id }))
    setLastSelected((prev) => (
      id !== null ? { entityType, id }
      : prev?.entityType === entityType ? null : prev
    ))
  }, [])

  const onSelectMulti = useCallback(
    (entityType: string, id: string, mode: SelectionMode, orderedIds: string[]) => {
      const cur = multiSel[entityType] ?? []
      const { next, anchor } = applySelect(cur, id, mode, orderedIds, anchorRef.current[entityType] ?? null)
      anchorRef.current[entityType] = anchor
      setMultiSel((s) => ({ ...s, [entityType]: next }))
      if (next.length === 0) {
        // 选中集清空(再击取消/Escape/空框选)→ primary 指针与 lastSelected 一致清空(F1,属性面板不残留)
        setSelection((s) => ({ ...s, [entityType]: null }))
        setLastSelected((prev) => (prev?.entityType === entityType ? null : prev))
      } else {
        setSelection((s) => ({ ...s, [entityType]: id })) // active 指针跟随最后点击
        setLastSelected({ entityType, id })               // 跨类型最近选中同步跟随
      }
    },
    [multiSel],
  )

  return {
    selection, multiSelection: multiSel, lastSelected, currentTime, highlights,
    onSelect, onSelectMulti, setCurrentTime, setHighlights, clearHighlights, reset,
  }
}
