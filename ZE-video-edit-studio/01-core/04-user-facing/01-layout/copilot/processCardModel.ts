/**
 * §7.6 P0-C 过程卡的纯逻辑(与 JSX 分离,vitest 可测)。
 * 把本轮 change_set 摘要归纳成「过程视图」——按实体类型分组的步骤行(生命周期变更 vs 字段更新),
 * 供 AssistantStage 事后(post-hoc)渲染「AI 这一轮做了什么」。数据源复用变更卡的 ChangeCardSummary。
 */
import type { ChangeCardSummary } from './changeCardModel'

/** 一类实体的过程步骤:该类型下有几处生命周期变更(增/删)、几处纯字段更新。 */
export type ProcessStep = {
  entity_type: string
  lifecycle_count: number
  update_count: number
}

export type ProcessCardView = {
  steps: ProcessStep[]
  total: number
  reversible: number
  irreversible: number
  isEmpty: boolean
}

/** change_set 摘要 → 过程视图。按 entity_type 分组、按类型名排序(稳定);空摘要 → isEmpty。 */
export function buildProcessCardView(cs: ChangeCardSummary | null | undefined): ProcessCardView {
  const byType = new Map<string, ProcessStep>()
  for (const e of cs?.entities ?? []) {
    const step =
      byType.get(e.entity_type) ?? { entity_type: e.entity_type, lifecycle_count: 0, update_count: 0 }
    if (e.lifecycle) step.lifecycle_count += 1
    else step.update_count += 1
    byType.set(e.entity_type, step)
  }
  const steps = [...byType.values()].sort((a, b) => a.entity_type.localeCompare(b.entity_type))
  return {
    steps,
    total: cs?.count ?? 0,
    reversible: cs?.reversible_count ?? 0,
    irreversible: cs?.irreversible_count ?? 0,
    isEmpty: (cs?.count ?? 0) === 0,
  }
}
