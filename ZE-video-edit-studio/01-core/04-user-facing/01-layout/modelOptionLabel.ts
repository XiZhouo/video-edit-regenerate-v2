/**
 * modelOptionLabel — 模型下拉一行文字的拼装(纯逻辑,可单测)。
 *
 * 一行里最多四段:模型名 · 来源 · 计费方式 · 推荐/不可用原因。
 *
 * 关键一条:**来源与计费方式经常是同一句话**。org 渠道的 source='org'、billing_hint='org_provided',
 * 而 `ai.source.org` 与 `ai.billing.org_provided` 在 zh/en 里都译成同一个词 —— 直接拼就成了
 * 「Banana Pro · 组织提供 · 组织提供」。同一件事说两遍不增加信息,只挤掉本来就不宽的下拉。
 * 所以这里按**渲染结果**去重(而不是按 key 判断),换语言、加渠道都不会再漏。
 */

export type ModelOptionLike = {
  model_id: string
  model_label: string
  available?: boolean
  source?: string | null
  billing_hint?: string | null
  reason?: string | null
}

export function modelOptionLabel(
  option: ModelOptionLike,
  t: (key: string) => string,
  opts: { recommended?: boolean } = {},
): string {
  const segments: string[] = [String(option.model_label ?? '').trim() || option.model_id]
  if (option.available) {
    for (const key of [
      option.source ? `ai.source.${option.source}` : '',
      option.billing_hint ? `ai.billing.${option.billing_hint}` : '',
    ]) {
      if (!key) continue
      const text = String(t(key) ?? '').trim()
      // 去重按渲染结果:来源与计费译成同一句话时只说一次。
      if (text && !segments.includes(text)) segments.push(text)
    }
  }
  let line = segments.join(' · ')
  if (option.available && opts.recommended) line += ` ${t('ai.picker.recommended')}`
  if (!option.available) line += ` ${t(`ai.error.${option.reason ?? 'requires_connection'}`)}`
  return line
}
