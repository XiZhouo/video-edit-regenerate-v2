/**
 * CapabilityModelPicker — Build 唯一受管模型选择器(规划 §3.6)。
 *
 * 所有工具必须复用它,禁止自建 provider/model 下拉。消费 useAiOptions(GET /api/ai/options):
 * 「显示所有可能性、只区别显示能用的、只能选能用的」——全部候选模型都渲染,不可用项 <option
 * disabled> + 9 类统一错误码原因;无任何可用时旁给「管理 AI 连接」一键直达设置中心。
 *
 * §3.5「记住我的选择」:把当前选中存成该功能的私有默认(FeaturePreference)。此前这个控件
 * **不存在** —— 设置中心「能力与默认项」页的注释和空态文案都写着「设默认在各工具选择器
 * 『记住我的选择』」,但全仓没有任何地方调用过 apiSetPreference(…, 非空),于是那页永远是空的,
 * 而规划里「保存过的选择失效怎么办」这条规则也永远触发不了。
 *
 * 保存过的选择失效时:服务端 resolver 硬阻断(current=null + preference_blocked),这里
 * **保留原值显示 + 置灰执行 + 提示更换**,不静默改选(§3.5「不能偷偷改项目数据」)。
 *
 * 用法:
 *   <CapabilityModelPicker
 *     query={{ featureId: 'foundry.image.generate', capability: 'image', require: ['image.generate'] }}
 *     value={modelId} onChange={setModelId} />
 */
import React from 'react'
import { useT } from '../05-preferences'
import { useAiOptions, apiSetPreference, type AiOptionsQuery } from '../03-authentication/aiOptions'
import { requestOpenCredentials } from '../03-authentication/aiRoutes'
import type { AiSettingsTarget } from '../03-authentication/aiSettingsLink'
import { modelOptionLabel } from './modelOptionLabel'

export function CapabilityModelPicker({ query, value, onChange, disabled, allowDefault }: {
  query: AiOptionsQuery
  /** 受控选中的 model_id;缺省用 snapshot.current(auto/意图解析结果)。 */
  value?: string
  onChange?: (modelId: string) => void
  disabled?: boolean
  /** 声明后在候选前置一个「用项目默认」空值选项(value="",恒可选);供 per-item 覆盖场景
   *  ——空=跟随项目默认模型,非空=本条覆盖。不声明则无此项(行为不变)。 */
  allowDefault?: { label: string }
}): React.ReactNode {
  const t = useT()
  const { snapshot, loaded, refresh } = useAiOptions(query)
  const [saving, setSaving] = React.useState(false)
  const [justSaved, setJustSaved] = React.useState(false)

  if (!loaded || !snapshot) {
    return <select className="ze-select" disabled aria-busy="true" />
  }
  const options = snapshot.options
  // allowDefault 时,空值(用项目默认)恒是合法选择;否则缺省回落 snapshot.current(auto)。
  const selected = value ?? (allowDefault ? '' : snapshot.current) ?? ''
  // allowDefault 存在时「用项目默认」永远可选,故不算「无可用」(不置灰、不必挂管理入口)。
  const noneAvailable = !snapshot.has_available && !allowDefault
  // 保存过的选择失效:保留原值给用户看见,执行置灰,提示更换(§3.5)。
  const savedBlocked = snapshot.preference_blocked ? snapshot.preference : null
  // 「记住我的选择」只在选中了一个**具体且可用**的模型时有意义(空值=跟随项目默认,不存)。
  const selectedBinding = options.find((o) => o.model_id === selected)
  const canRemember = !!selected && !!selectedBinding?.available && selected !== snapshot.preference

  return (
    // data-wrap:窄栏(如批次设置面板 ~190px)里不换行的话,select 会被挤到 121px、
    // 模型名从中间截断,「管理 AI 连接」也被压成两行。换行后 select 独占一行、按钮落下一行。
    <span className="ze-row" data-gap="2" data-align="center" data-wrap="true">
      <select
        className="ze-select"
        data-line="full"
        value={selected}
        disabled={disabled}
        data-ai-none-available={noneAvailable ? 'true' : undefined}
        onChange={(e) => onChange?.(e.target.value)}
      >
        {allowDefault && <option value="">{allowDefault.label}</option>}
        {options.map((o) => (
          <option key={o.model_id} value={o.model_id} disabled={!o.available}>
            {/* §3.6:名 · 来源 · 计费方式 · 推荐/不可用原因。拼装与去重在 modelOptionLabel(已单测)——
                来源与计费常译成同一句话,直接拼会出现「Banana Pro · 组织提供 · 组织提供」。 */}
            {modelOptionLabel(o, t, { recommended: !value && o.model_id === snapshot.current })}
          </option>
        ))}
      </select>
      {/* §3.6:每个选择器旁『固定』提供「管理 AI 连接」深链;无可用时更醒目(data-emphasis)。 */}
      {/* 落点跟着 query 走:一个都不可用时该去「连接方式」绑账号,否则是来调这个能力的默认项 */}
      <button type="button" className="ze-link" data-emphasis={noneAvailable ? 'true' : undefined}
              onClick={() => requestOpenCredentials({
                page: noneAvailable ? 'connect' : 'caps',
                capability: query.capability,
              } satisfies AiSettingsTarget)}>
        {t('ai.picker.manage')}
      </button>
      {canRemember && (
        <button type="button" className="ze-link" data-ai-remember="true" disabled={saving}
                onClick={() => {
                  setSaving(true)
                  void apiSetPreference(query.featureId, query.capability, selected)
                    .then(() => { setJustSaved(true); refresh() })
                    .catch(() => {})
                    .finally(() => setSaving(false))
                }}>
          {t('ai.picker.remember')}
        </button>
      )}
      {justSaved && !canRemember && (
        <span className="ze-text" data-size="xs" data-ink="tertiary">{t('ai.picker.remembered')}</span>
      )}
      {savedBlocked && (
        // 原值必须留在界面上 —— 用户得看见「我存的是它、它现在不能用」,而不是发现选中项莫名变了
        // 属性带**原因码**而不是 model id:id 已经在可见文案里了,属性再放一遍是冗余;
        // 自动化/排查真正需要的是「为什么用不了」这一维。
        <span className="ze-text" data-size="xs" data-ink="tertiary"
              data-ai-saved-blocked={snapshot.preference_blocked ?? undefined}>
          {t('ai.picker.savedLabel')}{savedBlocked} · {t('ai.picker.savedBlocked')}
        </span>
      )}
    </span>
  )
}
