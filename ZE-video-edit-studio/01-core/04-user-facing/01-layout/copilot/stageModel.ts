/**
 * §7.3 P0-C AssistantStage 的纯逻辑大脑(与 JSX 分离,vitest 可测)。
 *
 * 把「连接态 × turn 态 × 待确认 × 本轮变更」映射成一份舞台视图:决定精灵形态、显示哪些卡
 * (过程/变更/确认)、能否整轮撤销、状态文案键。AssistantStage 合并了旧 CopilotDock、账户引导、
 * 确认提示与引导层——这些分支此前散在多组件的 useState 里,本模型把「该显什么」收敛成一处可测纯逻辑,
 * JSX 只按视图渲染。覆盖 §7.5 验收诸态:无连接空态 / 凭证过期 / 纯回答 / 多工具多改动 / 待确认 /
 * 中途取消 / 失败。
 *
 * 过程卡两面:turn 进行中 = 实时状态(正在理解/调用/等待,由 turnState 驱动);turn 完成 =
 * 事后摘要(processCardModel.buildProcessCardView 按实体归纳「做了什么」)。二者同一张卡、不同数据面。
 */
import { hasUndoable, type ChangeCardSummary } from './changeCardModel'
import type { HubErrorCode } from '@ze/core/04-user-facing/03-authentication/aiOptions'

/** assistant_turn.py 的 7 个稳定状态;null=当前无活跃 turn。 */
export type TurnState =
  | 'created' | 'planning' | 'acting' | 'awaiting_confirmation'
  | 'completed' | 'cancelled' | 'failed'

/** AI 连接可用性(源自 AiOptionsSnapshot.has_available + resolver 错误码)。 */
export type StageConnection = {
  /** 是否有可用 AI 连接与模型(has_available)。false → 舞台进空态。 */
  available: boolean
  /** 不可用时的具体原因(requires_connection / credential_expired / …),用于空态措辞。 */
  error?: HubErrorCode | null
}

export type StageInput = {
  connection: StageConnection
  turnState: TurnState | null
  change: ChangeCardSummary | null
  /** server 侧是否有待人审的 confirmation(EV_CONFIRMATION status=pending)。 */
  pendingConfirmation: boolean
}

export type StageKind =
  | 'no_connection' | 'idle' | 'thinking' | 'awaiting_confirmation'
  | 'done' | 'cancelled' | 'failed'

/** 精灵形态:idle=静默存在;busy=运转中;attention=需用户关注(待确认)。 */
export type SpriteMode = 'idle' | 'busy' | 'attention'

export type StageView = {
  kind: StageKind
  spriteMode: SpriteMode
  showProcessCard: boolean
  showChangeCard: boolean
  showConfirmCard: boolean
  canUndoTurn: boolean
  /** 空态原因(仅 kind=no_connection 时非 null):区分「未连接」「凭证过期」「无可用模型」。 */
  emptyReason: 'requires_connection' | 'credential_expired' | 'unavailable' | null
  /** 状态行 i18n 键。 */
  statusKey: string
}

const _ACTIVE = new Set<TurnState>(['created', 'planning', 'acting'])

/** 舞台输入 → 视图。纯函数;连接优先:无连接直接空态,不看 turn。 */
export function buildStageView(input: StageInput): StageView {
  const { connection, turnState, change, pendingConfirmation } = input

  if (!connection.available) {
    const reason =
      connection.error === 'credential_expired'
        ? 'credential_expired'
        : connection.error === 'requires_connection'
          ? 'requires_connection'
          : 'unavailable'
    return {
      kind: 'no_connection',
      spriteMode: 'idle',
      showProcessCard: false,
      showChangeCard: false,
      showConfirmCard: false,
      canUndoTurn: false,
      emptyReason: reason,
      statusKey: `assistant.status.${reason}`,
    }
  }

  if (turnState === null) {
    return {
      kind: 'idle',
      spriteMode: 'idle',
      showProcessCard: false,
      showChangeCard: false,
      showConfirmCard: false,
      canUndoTurn: false,
      emptyReason: null,
      statusKey: 'assistant.status.idle',
    }
  }

  if (turnState === 'awaiting_confirmation') {
    return {
      kind: 'awaiting_confirmation',
      spriteMode: 'attention',
      showProcessCard: true,
      showChangeCard: false,
      showConfirmCard: pendingConfirmation,
      canUndoTurn: false,
      emptyReason: null,
      statusKey: 'assistant.status.awaiting_confirmation',
    }
  }

  if (_ACTIVE.has(turnState)) {
    return {
      kind: 'thinking',
      spriteMode: 'busy',
      showProcessCard: true,
      showChangeCard: false,
      showConfirmCard: false,
      canUndoTurn: false,
      emptyReason: null,
      statusKey: `assistant.status.${turnState}`,
    }
  }

  // 终态:completed / cancelled / failed(此处 turnState 逻辑上只余这三个;显式逐字面量映射,
  // 不依赖 Set.has 的类型收窄——每支都产 StageKind 字面量,cancelled/failed 原样、completed→done)。
  const hasChange = (change?.count ?? 0) > 0
  const kind: StageKind =
    turnState === 'completed' ? 'done' : turnState === 'cancelled' ? 'cancelled' : 'failed'
  return {
    kind,
    spriteMode: 'idle',
    showProcessCard: true,
    showChangeCard: kind === 'done' && hasChange,
    showConfirmCard: false,
    // 撤销只对 completed 且有可逆改动开放(cancelled/failed 不给整轮撤销)
    canUndoTurn: kind === 'done' && hasUndoable(change),
    emptyReason: null,
    statusKey: `assistant.status.${kind}`,
  }
}
