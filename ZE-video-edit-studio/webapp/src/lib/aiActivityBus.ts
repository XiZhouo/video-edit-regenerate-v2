/**
 * aiActivityBus.ts — 「AI 在行动」事实帧的进程内派发点(与 guideBus 同型)。
 *
 * useDocument 主流把三类事实转投这里(单 SSE 连接,零 props 穿透):
 *   - ai_change:entity_change 且 source="ai"(AI 经命令总线改了实体);
 *   - pending:AI 发起的需确认命令(确认拉起帧,不含批准材料);
 *   - entity_status:运行态帧(供 useAiActivity 判定长任务起/止转变)。
 * useAiActivity 聚合定稿一条回执后,再发布 receipt_fired 供 App 高亮桥(F2)消费。
 *
 * 本总线只搬呈现事实,从不触碰命令——确认走既有网页 nonce 通道,与本总线无关。
 */
import type { ChangePayload, PendingConfirmationPayload, StatusPayload } from './api'

export type AiActivityFrame =
  | { kind: 'ai_change'; payload: ChangePayload }
  | { kind: 'pending'; payload: PendingConfirmationPayload }
  | { kind: 'entity_status'; payload: StatusPayload }
  /** 回执聚合定稿(useAiActivity 发布;App 的 F2 高亮桥消费,精灵不消费此帧)。 */
  | { kind: 'receipt_fired'; entities: { entityType: string; ids: string[] }[]; holdMs: number }

type Listener = (frame: AiActivityFrame) => void

const listeners = new Set<Listener>()

export function publishAiActivity(frame: AiActivityFrame): void {
  for (const listener of Array.from(listeners)) {
    try {
      listener(frame)
    } catch {
      /* 单个订阅者抛错不影响其余(纯视觉,不阻断主流) */
    }
  }
}

export function subscribeAiActivity(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
