/**
 * inspectorTarget — form 槽(属性面板)「此刻该显示谁」的纯解析(selection-follow,G-F 部分解冻)
 *
 * form 槽的 entity_bindings 是允许类型闭集;view_state.followSelection=true 时跟随
 * 跨类型「最近选中」(useInteractionStore.lastSelected 经 SlotContext 透传)。
 * 收成纯函数:renderSlot 只调用不内联逻辑(selectionModel 同款先例),回退链可单测。
 *
 * 回退链(确定性):
 *   1. follow && lastSelected ∈ bindings 且实体仍在 → 用它
 *   2. primary(bindings[0])的 selection —— follow=false 时与旧路径字节等价
 *   3. 按 bindings 顺序找首个 selection 非空且实体仍在的类型(多型槽不白屏)
 *   4. 单实体兜底:primary 类型恰好只有一个 live 实体 → 用它(与 slotAdapters.tsx 单物体回退对齐:
 *      单实体编辑器即应显示那一个,demo 打开 / 导入后立即可见,不需先手动点选;>1 实体仍要求显式选择
 *      以消歧义,绝不覆盖既有显式选中)
 *   5. null → 空态(slot.selectToEdit)
 */

import type { EntityDict } from '../03-editor'

export type InspectorTarget = { entityType: string; entity: EntityDict } | null

function liveEntity(
  entitiesByType: Record<string, EntityDict[]>,
  entityType: string,
  id: string | null | undefined,
): EntityDict | null {
  if (!id) return null
  return (entitiesByType[entityType] ?? []).find((e) => e.id === id) ?? null
}

export function resolveInspectorTarget(
  bindings: string[],
  follow: boolean,
  lastSelected: { entityType: string; id: string } | null,
  selection: Record<string, string | null>,
  entitiesByType: Record<string, EntityDict[]>,
): InspectorTarget {
  // 1. 跟随跨类型最近选中(实体被删/选择已失效 → 落入回退链,不白屏)
  if (follow && lastSelected && bindings.includes(lastSelected.entityType)) {
    const e = liveEntity(entitiesByType, lastSelected.entityType, lastSelected.id)
    if (e) return { entityType: lastSelected.entityType, entity: e }
  }
  // 2. primary 的 selection(旧路径)
  const primary = bindings[0]
  if (primary) {
    const e = liveEntity(entitiesByType, primary, selection[primary])
    if (e) return { entityType: primary, entity: e }
  }
  // 3. 其余绑定类型按序找首个有效选中
  for (const t of bindings.slice(1)) {
    const e = liveEntity(entitiesByType, t, selection[t])
    if (e) return { entityType: t, entity: e }
  }
  // 4. 单实体兜底:显式选择全空时,若 primary 类型恰好只有一个 live 实体 → 用它(对齐
  //    slotAdapters.tsx 的 `list.length === 1 ? list[0] : null`)。>1 实体不兜底(消歧义留给显式选择);
  //    纯加性:仅在 1-3 全空时才到这里,绝不覆盖显式选中。
  if (primary) {
    const live = (entitiesByType[primary] ?? []).filter((e) => !e.deleted)
    if (live.length === 1) return { entityType: primary, entity: live[0] }
  }
  return null
}
