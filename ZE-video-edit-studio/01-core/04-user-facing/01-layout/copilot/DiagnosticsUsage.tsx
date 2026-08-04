/**
 * §3.1 AI 设置中心「诊断与用量」页 —— 展示本工具近期 AI 调用的**实际落位**与用量(诊断用,非计费权威)。
 *
 * 数据源 = useRecentReceipts(GET /api/ai/receipts,InvocationLog 脱敏读半);归纳走纯逻辑
 * buildReceiptDigest(已单测)。本组件只把摘要映射成可读结构。分层:className + data-*,复用既有原子。
 */
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useRecentReceipts } from '@ze/core/04-user-facing/03-authentication/aiOptions'
import { buildReceiptDigest, type CountRow } from '@ze/core/04-user-facing/03-authentication/receiptDigest'

function CountList({ rows, keyField }: { rows: Array<CountRow & Record<string, unknown>>; keyField: string }) {
  const t = useT()
  return (
    <ul className="ze-copilot__diag-list">
      {rows.map((r) => (
        <li key={String(r[keyField])} className="ze-copilot__diag-row">
          <span className="ze-copilot__diag-key">{String(r[keyField]) || '—'}</span>
          <span className="ze-text" data-role="secondary">{t('copilotAccount.diag.okError', { ok: r.ok, error: r.error })}</span>
        </li>
      ))}
    </ul>
  )
}

export function DiagnosticsUsage() {
  const t = useT()
  const { receipts, loaded, refresh } = useRecentReceipts(50)
  const d = buildReceiptDigest(receipts)

  return (
    <section className="ze-copilot__diag" data-card="diagnostics" aria-label={t('copilotAccount.diag.title')}>
      <div className="ze-copilot__diag-head">
        <span className="ze-text" data-weight="semibold">{t('copilotAccount.diag.title')}</span>
        <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={refresh}>
          {t('copilotAccount.diag.refresh')}
        </button>
      </div>

      {loaded && d.total === 0 ? (
        <div className="ze-text" data-role="secondary">{t('copilotAccount.diag.empty')}</div>
      ) : (
        <>
          <div className="ze-copilot__diag-summary">
            <span className="ze-text" data-weight="semibold">{t('copilotAccount.diag.total', { total: d.total })}</span>
            <span className="ze-text" data-role="secondary">{t('copilotAccount.diag.okError', { ok: d.ok, error: d.error })}</span>
            {d.latency.count > 0 ? (
              <span className="ze-text" data-role="secondary">
                {t('copilotAccount.diag.latency', { avg: d.latency.avgMs, max: d.latency.maxMs })}
              </span>
            ) : null}
          </div>

          {d.mostRecent ? (
            <div className="ze-text" data-role="tertiary">
              {t('copilotAccount.diag.mostRecent', { provider: d.mostRecent.provider || '—', model: d.mostRecent.model_id || '—' })}
            </div>
          ) : null}

          {/* §3.5 步8 / §6.2D:解析轨迹 —— 为什么落到这个模型、auto 下是否换过渠道。
              轨迹是后端产的技术串(不经 t),这里只提供可读的标题与结构。 */}
          {d.mostRecent && d.mostRecent.trace.length > 0 ? (
            <div className="ze-copilot__diag-group">
              <div className="ze-text" data-role="secondary" data-weight="semibold">{t('copilotAccount.diag.whyThisModel')}</div>
              <ol className="ze-copilot__diag-list">
                {d.mostRecent.trace.map((step, i) => (
                  <li key={`${i}-${step}`} className="ze-copilot__diag-row">
                    <span className="ze-copilot__diag-key">{step}</span>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}

          {d.byProvider.length > 0 ? (
            <div className="ze-copilot__diag-group">
              <div className="ze-text" data-role="secondary" data-weight="semibold">{t('copilotAccount.diag.byProvider')}</div>
              <CountList rows={d.byProvider} keyField="provider" />
            </div>
          ) : null}

          {d.byModel.length > 0 ? (
            <div className="ze-copilot__diag-group">
              <div className="ze-text" data-role="secondary" data-weight="semibold">{t('copilotAccount.diag.byModel')}</div>
              <CountList rows={d.byModel} keyField="model_id" />
            </div>
          ) : null}

          {d.byErrorCode.length > 0 ? (
            <div className="ze-copilot__diag-group">
              <div className="ze-text" data-role="secondary" data-weight="semibold">{t('copilotAccount.diag.byErrorCode')}</div>
              <ul className="ze-copilot__diag-list">
                {d.byErrorCode.map((e) => (
                  <li key={e.code} className="ze-copilot__diag-row">
                    {/* 诊断技术视图:直接显 9 类统一错误码(rate_limited 等)+ unknown 兜底,不经 t(避免动态键/缺键) */}
                    <span className="ze-copilot__diag-key" data-code={e.code}>{e.code}</span>
                    <span className="ze-text" data-role="secondary">{e.count}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      )}
    </section>
  )
}
