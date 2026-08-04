/**
 * §3.1/§3.5 AI 设置中心「能力与默认项」页的纯逻辑(与 JSX 分离,vitest 可测)。
 *
 * 把 GET /api/ai/preferences(用户对每个工具的私有默认,keyed `"{feature_id} {capability}"`→model_id)
 * 与 GET /api/ai/lanes(用户全局能力泳道,capability→有序 model 列)归纳成展示结构,供页面渲染与
 * 逐条清除。resolver 五级优先(pin>preference>lane>intent>auto)里 preference/lane 是第 2/3 层。
 * 纯函数,不发请求;设置/清除动作由页面调 apiSetPreference/apiSetLane。
 */

export type PreferenceEntry = { key: string; featureId: string; capability: string; modelId: string }
export type LaneEntry = { capability: string; models: string[] }
export type PreferenceDigest = {
  preferences: PreferenceEntry[]
  lanes: LaneEntry[]
  /** 既无私有默认也无泳道 → 空态(页面显「暂未设置任何默认」)。 */
  isEmpty: boolean
}

/** preference store 复合键 `"{feature_id} {capability}"`(空格分隔,两者均无空格)→ 拆分。
 *  防御:无空格 → 整串当 featureId、capability 空;多空格按**首个**空格拆(feature 在前)。 */
function _parseKey(key: string): { featureId: string; capability: string } {
  const i = key.indexOf(' ')
  if (i < 0) return { featureId: key, capability: '' }
  return { featureId: key.slice(0, i), capability: key.slice(i + 1) }
}

/** preferences + lanes → 展示摘要。空/null 安全;preferences 按 featureId→capability 稳定排序,
 *  lanes 按 capability 稳定排序;model 为空串的 preference 跳过(等价未设)。 */
export function buildPreferenceDigest(
  prefs: Record<string, string> | null | undefined,
  lanes: Record<string, string[]> | null | undefined,
): PreferenceDigest {
  const preferences: PreferenceEntry[] = Object.entries(prefs ?? {})
    .filter(([, modelId]) => typeof modelId === 'string' && modelId !== '')
    .map(([key, modelId]) => ({ key, ...(_parseKey(key)), modelId }))
    .sort((a, b) => a.featureId.localeCompare(b.featureId) || a.capability.localeCompare(b.capability))

  const laneList: LaneEntry[] = Object.entries(lanes ?? {})
    .map(([capability, models]) => ({
      capability,
      models: Array.isArray(models) ? models.filter((m) => typeof m === 'string' && m !== '') : [],
    }))
    .filter((l) => l.models.length > 0)
    .sort((a, b) => a.capability.localeCompare(b.capability))

  return { preferences, lanes: laneList, isEmpty: preferences.length === 0 && laneList.length === 0 }
}
