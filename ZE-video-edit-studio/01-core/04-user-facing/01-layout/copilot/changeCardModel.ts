/**
 * §7.3/§7.4 P0-C 变更卡的纯逻辑(与 JSX 分离,vitest 可测)。
 * 变更卡数据源 = /api/ai/chat 响应的 change_set 摘要;撤销走 apiUndoTurn。
 */
import type { AiTurnUndoResult } from '@ze/core/04-user-facing/03-authentication/aiOptions'

/** /api/ai/chat 响应里的 change_set 紧凑摘要(server _changeset_response_summary 产)。 */
export type ChangeCardSummary = {
  count: number
  reversible_count: number
  irreversible_count: number
  entities: Array<{ entity_type: string; entity_id: string; fields: string[]; lifecycle: boolean }>
}

/** 本轮是否有可整轮撤销的改动(有可逆 op 才显示「撤销本轮」按钮)。 */
export function hasUndoable(cs: ChangeCardSummary | null | undefined): boolean {
  return !!cs && (cs.reversible_count ?? 0) > 0
}

/** 撤销结果 → {undone, kept}:undone=实际撤销的 op 数;kept=因冲突/不支持/失败而保留的数。 */
export function summarizeUndoResult(r: AiTurnUndoResult): { undone: number; kept: number } {
  return {
    undone: r.reverted?.length ?? 0,
    kept: (r.conflict?.length ?? 0) + (r.skipped?.length ?? 0) + (r.dispatch_failed?.length ?? 0),
  }
}
