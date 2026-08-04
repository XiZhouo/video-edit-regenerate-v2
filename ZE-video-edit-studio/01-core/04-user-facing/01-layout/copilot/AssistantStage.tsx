/**
 * §7.3 P0-C AssistantStage —— 助手「舞台」的卡区呈现容器。
 *
 * 把「连接态 × turn 态 × 待确认 × 本轮变更」经 buildStageView(纯逻辑,已单测)裁定后,编排三张卡:
 * - 过程卡(ProcessCard):turn 完成后『这一轮做了什么』事后摘要。
 * - 变更卡:本轮改动实体 chips(点击定位到实体)+ 整轮撤销按钮。
 * - 确认卡(ConfirmCard):待人审确认时『将执行/影响/为什么』+ 批准·稍后·取消。
 *
 * 纯呈现:不持状态、不发请求;开轮/撤销/确认动作全经 props 回调交回宿主(CopilotDock)。这样
 * 「该显什么」收敛在 stageModel,「怎么取数/落命令」留在宿主,JSX 只发 className+data-*。
 */
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { buildStageView, type StageConnection, type TurnState } from './stageModel'
import { buildProcessCardView } from './processCardModel'
import { ProcessCard } from './ProcessCard'
import { ConfirmCard } from './ConfirmCard'
import type { ConfirmCardView, ConfirmAction } from './confirmCardModel'
import type { ChangeCardSummary } from './changeCardModel'

export type AssistantStageProps = {
  connection: StageConnection
  turnState: TurnState | null
  change: ChangeCardSummary | null
  /** 待人审确认的视图(有则渲染确认卡);null=无待确认。 */
  confirmView?: ConfirmCardView | null
  undoing: boolean
  onUndoTurn: () => void
  onRevealEntity: (entityType: string, entityId: string, field?: string) => void
  /** §7.3 W1-4:本轮当前阶段(understanding/reading/tool/waiting/done);null=不在轮内。 */
  liveStage?: { stage: string; tool: string } | null
  onConfirmAction: (action: ConfirmAction, nonce: string) => void
}

const MAX_CHIPS = 6

export function AssistantStage({
  connection, turnState, change, confirmView,
  undoing, onUndoTurn, onRevealEntity, onConfirmAction, liveStage,
}: AssistantStageProps) {
  const t = useT()
  const view = buildStageView({
    connection,
    turnState,
    change,
    pendingConfirmation: !!confirmView,
  })

  // 无卡可显(空态/纯 idle/纯回答无改动)→ 不占位。
  if (!view.showProcessCard && !view.showChangeCard && !view.showConfirmCard) return null

  return (
    <div className="ze-copilot__stage" data-stage={view.kind}>
      {view.showProcessCard && change && (change.count ?? 0) > 0 ? (
        <ProcessCard view={buildProcessCardView(change)} />
      ) : null}

      {/* W1-4 过程卡:五段逐一显形(理解→读取→调用工具→等待→完成)。
          此前只有「完成」有渲染点,前四段既无事件也无文案,于是发出到拿回话之间
          用户只看得到一个转圈,不知道 AI 在干什么、卡在哪。 */}
      {liveStage && liveStage.stage ? (
        <div className="ze-copilot__context" data-card="process" data-stage={liveStage.stage}>
          <span className="ze-copilot__dots" aria-hidden="true"><i /><i /><i /></span>
          <span>{stageLabel(liveStage, t)}</span>
        </div>
      ) : null}

      {view.showConfirmCard && confirmView ? (
        <ConfirmCard view={confirmView} onAction={onConfirmAction} />
      ) : null}

      {view.showChangeCard && change ? (
        <div className="ze-copilot__context" data-card="change">
          <div className="ze-copilot__changecard-row">
            <span>{t('copilot.changedThisTurn', { count: change.count })}</span>
            {view.canUndoTurn ? (
              <button type="button" className="ze-button" data-variant="ghost" disabled={undoing}
                      onClick={onUndoTurn}>
                {undoing ? t('copilot.undoing') : t('copilot.undoTurn')}
              </button>
            ) : null}
          </div>
          {change.entities.length > 0 ? (
            <div className="ze-copilot__changechips">
              {/* W1-6:改了哪几个字段就出几个 chip —— 规划要求「定位到实体/字段」,
                  此前整条实体一个 chip、点了只能选中实体,字段那一半从没做。
                  改一个实体的三个字段,用户要能直接跳到出问题的那个字段上。
                  lifecycle(增删实体)没有字段可跳,仍出一个实体级 chip。 */}
              {change.entities.slice(0, MAX_CHIPS).flatMap((e) => (
                e.lifecycle || e.fields.length === 0
                  ? [(
                    <button key={`${e.entity_type}:${e.entity_id}`} type="button" className="ze-copilot__changechip"
                            title={t('copilot.locateEntity')}
                            onClick={() => onRevealEntity(e.entity_type, e.entity_id)}>
                      {e.entity_type}
                      {e.lifecycle ? ` · ${t('copilot.entityLifecycle')}` : ''}
                    </button>
                  )]
                  : e.fields.slice(0, MAX_CHIPS).map((f) => (
                    <button key={`${e.entity_type}:${e.entity_id}:${f}`} type="button"
                            className="ze-copilot__changechip"
                            title={t('copilot.locateField')}
                            onClick={() => onRevealEntity(e.entity_type, e.entity_id, f)}>
                      {`${e.entity_type} · ${f}`}
                    </button>
                  ))
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/** 阶段 → 人话。封闭集之外一律返空(宁可不渲染,也不把 'understanding' 这种裸词摆给用户看)。 */
function stageLabel(live: { stage: string; tool: string }, t: ReturnType<typeof useT>): string {
  switch (live.stage) {
    case 'understanding': return t('copilot.stageUnderstanding')
    case 'reading': return t('copilot.stageReading')
    case 'tool': return t('copilot.stageTool', { tool: live.tool || '…' })
    case 'waiting': return t('copilot.stageWaiting')
    case 'done': return t('copilot.stageDone')
    default: return ''
  }
}
