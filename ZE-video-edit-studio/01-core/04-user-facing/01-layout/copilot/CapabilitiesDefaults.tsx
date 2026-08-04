/**
 * §3.1/§3.5 AI 设置中心「能力与默认项」页 —— 看/清用户的私有默认(每工具)与全局能力优先序。
 *
 * 数据源 = GET /api/ai/preferences + /api/ai/lanes;归纳走纯逻辑 buildPreferenceDigest(已单测)。
 * 设默认在各工具选择器「记住我的选择」;本页负责集中查看 + 逐条清除(回到 auto/意图)。
 * 分层:className + data-*,复用诊断页的通用列表布局类(ze-copilot__diag-*),不新增 CSS。
 */
import { useCallback, useEffect, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import {
  apiGetPreferences, apiGetLanes, apiSetPreference, apiSetLane,
} from '@ze/core/04-user-facing/03-authentication/aiOptions'
import { buildPreferenceDigest } from '@ze/core/04-user-facing/03-authentication/preferenceDigest'

export function CapabilitiesDefaults() {
  const t = useT()
  const [prefs, setPrefs] = useState<Record<string, string>>({})
  const [lanes, setLanes] = useState<Record<string, string[]>>({})
  const [loaded, setLoaded] = useState(false)

  const load = useCallback(() => {
    Promise.all([apiGetPreferences(), apiGetLanes()])
      .then(([p, l]) => { setPrefs(p); setLanes(l); setLoaded(true) })
      .catch(() => setLoaded(true))
  }, [])
  useEffect(() => { load() }, [load])

  const d = buildPreferenceDigest(prefs, lanes)

  return (
    <section className="ze-copilot__diag" data-card="caps" aria-label={t('copilotAccount.tabCaps')}>
      {loaded && d.isEmpty ? (
        <div className="ze-text" data-role="secondary">{t('copilotAccount.caps.empty')}</div>
      ) : (
        <>
          {d.preferences.length > 0 ? (
            <div className="ze-copilot__diag-group">
              <div className="ze-text" data-role="secondary" data-weight="semibold">{t('copilotAccount.caps.prefsTitle')}</div>
              <ul className="ze-copilot__diag-list">
                {d.preferences.map((p) => (
                  <li key={p.key} className="ze-copilot__diag-row">
                    <span className="ze-copilot__diag-key">
                      {p.featureId}{p.capability ? ` · ${p.capability}` : ''} → {p.modelId}
                    </span>
                    <button type="button" className="ze-button" data-size="sm" data-variant="ghost"
                      onClick={() => { void apiSetPreference(p.featureId, p.capability, '').then(load).catch(() => {}) }}>
                      {t('copilotAccount.caps.clear')}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {d.lanes.length > 0 ? (
            <div className="ze-copilot__diag-group">
              <div className="ze-text" data-role="secondary" data-weight="semibold">{t('copilotAccount.caps.lanesTitle')}</div>
              <ul className="ze-copilot__diag-list">
                {d.lanes.map((l) => (
                  <li key={l.capability} className="ze-copilot__diag-row">
                    <span className="ze-copilot__diag-key">{l.capability}: {l.models.join(' › ')}</span>
                    <button type="button" className="ze-button" data-size="sm" data-variant="ghost"
                      onClick={() => { void apiSetLane(l.capability, []).then(load).catch(() => {}) }}>
                      {t('copilotAccount.caps.clear')}
                    </button>
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
