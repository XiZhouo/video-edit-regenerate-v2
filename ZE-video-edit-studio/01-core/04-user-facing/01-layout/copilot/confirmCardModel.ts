/**
 * §7.3 P0-C 确认卡的纯逻辑(与 JSX 分离,vitest 可测)。
 * 数据源 = turn 的 EV_CONFIRMATION 事件(assistant_turn.py:nonce/描述/影响/状态);
 * 供 AssistantStage 在 turn 处于 awaiting_confirmation 时渲染「说明要做什么、影响、为什么
 * 需要确认;批准/稍后/取消」。批准/取消经 nonce 回到 server 解析;稍后仅前端折叠、不释放 nonce。
 *
 * 不可撤销外部效果(已花额度的生成/已发出的调用)在批准前必须显式警示——§7.4「已发生的付费
 * 调用不进入普通 undo」的前置告知半边:让用户在按下批准前就看到「这步不可撤」。
 */

/** EV_CONFIRMATION 载荷(server 侧 assistant_turn 记录人审确认时的稳定字段)。 */
export type ConfirmCardPayload = {
  nonce: string
  description: string
  impact: string
  /** 为什么需要人工确认(可选;缺省用通用兜底文案)。 */
  reason?: string
  /** pending=待处理可操作;approved/cancelled/rejected=已解析,卡转只读。 */
  status: string
  /** 本次待批操作里带外部信封的不可撤销 op(split_changeset_reversibility 的 irreversible 组同形)。 */
  irreversible?: Array<{ operation_id?: string; kind: string; command_id?: string }>
}

/** 确认卡可用动作。later=稍后(仅折叠,不释放 nonce);approve/cancel=经 nonce 回 server。 */
export type ConfirmAction = 'approve' | 'later' | 'cancel'

export type ConfirmCardView = {
  nonce: string
  what: string
  impact: string
  /** server 给的「为什么需要确认」原文(turn 内容/数据);缺省时为空,由 whyKey 兜底。 */
  why: string
  /** why 为空时前端用的兜底文案 i18n 键(模型不硬编码显示文本)。 */
  whyKey: string
  /** pending 时给三个动作;已解析时为空(卡只读)。 */
  actions: ConfirmAction[]
  /** 待批操作是否含不可撤销外部效果(true 时 UI 必须显式警示,不可静默批准)。 */
  hasIrreversible: boolean
  /** 不可撤销效果的去重 kind 列表(如 ai_generation / outbound_api),按名排序稳定。 */
  irreversibleKinds: string[]
  isPending: boolean
  isResolved: boolean
}

const _RESOLVED = new Set(['approved', 'cancelled', 'canceled', 'rejected', 'released'])
/** why 缺省时的兜底文案 i18n 键(显示文本在 05-i18n,模型只给键)。 */
export const CONFIRM_WHY_FALLBACK_KEY = 'assistant.confirm.why_fallback'

/** EV_CONFIRMATION 载荷 → 确认卡视图。纯函数;status 不认识时保守当已解析(不给可操作动作)。 */
export function buildConfirmCardView(payload: ConfirmCardPayload): ConfirmCardView {
  const status = (payload.status ?? '').toLowerCase()
  const isPending = status === 'pending'
  const isResolved = _RESOLVED.has(status) || (!isPending && status !== '')
  const kinds = [...new Set((payload.irreversible ?? []).map((x) => x.kind).filter(Boolean))].sort()
  return {
    nonce: payload.nonce ?? '',
    what: payload.description ?? '',
    impact: payload.impact ?? '',
    why: payload.reason?.trim() ? payload.reason.trim() : '',
    whyKey: CONFIRM_WHY_FALLBACK_KEY,
    actions: isPending ? ['approve', 'later', 'cancel'] : [],
    hasIrreversible: kinds.length > 0,
    irreversibleKinds: kinds,
    isPending,
    isResolved,
  }
}
