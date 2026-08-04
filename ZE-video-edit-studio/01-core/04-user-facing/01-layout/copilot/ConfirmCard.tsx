/**
 * §7.3 P0-C 确认卡 —— turn 处于 awaiting_confirmation 且有 pending confirmation 时,AssistantStage
 * 渲染本卡:说明「将执行什么 / 影响 / 为什么需要确认」,给「批准 / 稍后 / 取消」。
 *
 * 纯呈现:视图数据来自 buildConfirmCardView(纯逻辑,已单测),本组件只把它映射成可读结构 + 动作。
 * 不自造表单、不碰凭证(账号配置一律走 AI 设置中心)。含不可撤销外部效果时显式警示(§7.4 前置告知)。
 * 分层:只发 className + data-*,复用既有 ze-copilot__/ze-button 视觉,无内联样式。
 */
import { useT } from '@ze/core/04-user-facing/05-preferences'
import type { ConfirmCardView, ConfirmAction } from './confirmCardModel'

export type ConfirmCardProps = {
  view: ConfirmCardView
  /** 动作回调:approve/cancel 经 nonce 回 server;later 仅前端折叠(不释放 nonce)。 */
  onAction: (action: ConfirmAction, nonce: string) => void
}

export function ConfirmCard({ view, onAction }: ConfirmCardProps) {
  const t = useT()
  return (
    <div className="ze-copilot__context" data-card="confirm" role="group" aria-label={t('assistant.confirm.title')}>
      <div className="ze-copilot__confirm-title">{t('assistant.confirm.title')}</div>
      <dl className="ze-copilot__confirm-fields">
        <dt>{t('assistant.confirm.whatLabel')}</dt>
        <dd data-field="what">{view.what}</dd>
        <dt>{t('assistant.confirm.impactLabel')}</dt>
        <dd data-field="impact">{view.impact}</dd>
        <dt>{t('assistant.confirm.whyLabel')}</dt>
        <dd data-field="why">{view.why || t(view.whyKey)}</dd>
      </dl>
      {view.hasIrreversible ? (
        <div className="ze-copilot__confirm-warn" data-emphasis="true" role="alert">
          {t('assistant.confirm.irreversibleWarn')}
        </div>
      ) : null}
      {view.actions.length > 0 ? (
        <div className="ze-copilot__confirm-actions">
          {view.actions.map((a) => (
            <button
              key={a}
              type="button"
              className="ze-button"
              data-variant={a === 'approve' ? 'primary' : 'ghost'}
              data-action={a}
              onClick={() => onAction(a, view.nonce)}
            >
              {t(`assistant.confirm.${a}`)}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
