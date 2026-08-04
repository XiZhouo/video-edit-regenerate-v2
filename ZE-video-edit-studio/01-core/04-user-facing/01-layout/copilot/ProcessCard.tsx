/**
 * §7.3 P0-C 过程卡(事后面)—— turn 完成后 AssistantStage 渲染「这一轮做了什么」:按实体类型分组的
 * 步骤行(几处增删 / 几处字段更新)+ 本轮总改动与可撤性摘要。
 *
 * 过程卡两面:进行中的实时状态(正在理解/调用/等待)由 AssistantStage 的状态行(stageModel.statusKey)
 * 承载;本组件是**事后摘要**面,视图数据来自 buildProcessCardView(纯逻辑,已单测)。
 * 分层:只发 className + data-*,复用 ze-copilot__ 视觉,无内联样式。
 */
import { useT } from '@ze/core/04-user-facing/05-preferences'
import type { ProcessCardView } from './processCardModel'

export type ProcessCardProps = { view: ProcessCardView }

export function ProcessCard({ view }: ProcessCardProps) {
  const t = useT()
  return (
    <div className="ze-copilot__context" data-card="process" role="group" aria-label={t('assistant.process.title')}>
      <div className="ze-copilot__process-title">{t('assistant.process.title')}</div>
      {view.isEmpty ? (
        <div className="ze-copilot__process-empty">{t('assistant.process.empty')}</div>
      ) : (
        <>
          <ul className="ze-copilot__process-steps">
            {view.steps.map((s) => (
              <li key={s.entity_type} className="ze-copilot__process-step" data-type={s.entity_type}>
                <span className="ze-copilot__process-type">{s.entity_type}</span>
                {s.lifecycle_count > 0 ? (
                  <span className="ze-copilot__process-count" data-kind="lifecycle">
                    {t('assistant.process.lifecycleN', { count: s.lifecycle_count })}
                  </span>
                ) : null}
                {s.update_count > 0 ? (
                  <span className="ze-copilot__process-count" data-kind="update">
                    {t('assistant.process.updateN', { count: s.update_count })}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
          <div className="ze-copilot__process-summary">
            {t('assistant.process.summary', {
              total: view.total,
              reversible: view.reversible,
              irreversible: view.irreversible,
            })}
          </div>
        </>
      )}
    </div>
  )
}
