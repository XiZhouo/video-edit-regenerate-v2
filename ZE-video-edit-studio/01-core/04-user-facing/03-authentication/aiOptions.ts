/**
 * ProviderHub 能力选项前端数据源(规划 §3.6 CapabilityModelPicker 的 hook)。
 *
 * 消费 GET /api/ai/options?feature_id&capability&require:返当前用户在当前功能下的可用**模型**
 * 投影(EffectiveCapabilitySnapshot)——每个候选带可用性 + 9 类统一错误码 + fix + i18n key。
 * 是 useAiRoutes 的模型级超集(路线级门控见 aiRoutes.ts)。CapabilityModelPicker 用它渲染
 * 「显示所有可能性、只区别显示能用的、只能选能用的」下拉。
 */
import { useCallback, useEffect, useState } from 'react'
import { authHeader, triggerAuthError } from './authApi'

/** 9 类统一错误码(与后端 sdk/hub/errors.py 一一对应;前端不再各自造 reason)。 */
export type HubErrorCode =
  | 'requires_connection' | 'credential_expired' | 'model_unavailable'
  | 'capability_mismatch' | 'quota_blocked' | 'provider_unreachable'
  | 'rate_limited' | 'request_invalid' | 'policy_denied'

export type HubFix = { action: string; [k: string]: unknown }

export type ModelOption = {
  model_id: string
  model_label: string
  route: string
  descriptor_id: string
  connection_id: string | null
  available: boolean
  capabilities: string[]
  reason: HubErrorCode | null
  reason_i18n: string | null
  fix: HubFix | null
  /** §3.6 连接来源类别(subscription/api/org/local);前端出 i18n 人读标签,不泄漏内部 kind。 */
  source?: string
  /** §3.6 该模型参数 schema 引用(有则选择器按选中 binding schema 驱动参数)。 */
  param_schema?: string
  /** §3.6 预估计费方式(性质码:subscription_quota/metered/org_provided/local_free);
   *  前端出 i18n 文案 `ai.billing.{hint}`,只说计费性质、不承诺精确价格。 */
  billing_hint?: string
}

export type AiOptionsSnapshot = {
  feature_id: string
  capability: string
  options: ModelOption[]
  pin: string | null
  current: string | null
  has_available: boolean
  /** 用户保存过的每功能选择(「记住我的选择」)。 */
  preference?: string | null
  /** 保存过的选择为什么用不了(9 类码之一)。非空 ⟹ current 必为 null:
   *  §3.5 要求保留原值供用户看见、禁用执行并提示更换,而不是悄悄换一个。 */
  preference_blocked?: string | null
  trace: string[]
}

export type AiOptionsQuery = {
  featureId: string
  capability: string
  require?: string[]
  pin?: string
  intent?: string
  /** §3.4/§3.5 私有覆盖(用户/工具层默认);优先级低于 pin、高于工具意图。 */
  preference?: string
}

export type AiOptionsState = {
  snapshot: AiOptionsSnapshot | null
  loaded: boolean
  refresh: () => void
}

export async function apiGetAiOptions(q: AiOptionsQuery): Promise<AiOptionsSnapshot> {
  const params = new URLSearchParams({ feature_id: q.featureId, capability: q.capability })
  if (q.require && q.require.length) params.set('require', q.require.join(','))
  if (q.pin) params.set('pin', q.pin)
  if (q.intent) params.set('intent', q.intent)
  if (q.preference) params.set('preference', q.preference)
  const res = await fetch(`/api/ai/options?${params.toString()}`, { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-options ${res.status}`)
  return res.json()
}

export function useAiOptions(q: AiOptionsQuery): AiOptionsState {
  const [snapshot, setSnapshot] = useState<AiOptionsSnapshot | null>(null)
  const [loaded, setLoaded] = useState(false)
  const key = `${q.featureId}|${q.capability}|${(q.require || []).join(',')}|${q.pin || ''}|${q.intent || ''}|${q.preference || ''}`

  const refresh = useCallback(() => {
    apiGetAiOptions(q)
      .then((s) => { setSnapshot(s); setLoaded(true) })
      .catch(() => setLoaded(true))   // 拿不到时不硬锁(驱动层预检仍兜底,同 useAiRoutes)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  useEffect(() => { refresh() }, [refresh])
  return { snapshot, loaded, refresh }
}

// ── 最近 AI 调用回执(§2.2「当前工具最近调用的实际 provider/model」)──────────────
// 消费 GET /api/ai/receipts(InvocationLog 读半,脱敏);访客/无记录返空。诊断展示用,非计费权威。
export type AiReceipt = {
  receipt_id: string
  feature_id: string
  capability: string
  provider: string
  model_id: string
  route: string
  status: string
  created_at: number
  latency_ms?: number
  error_code?: string
  params_summary?: Record<string, unknown>
  result_summary?: Record<string, unknown>
  /** §3.5 步8 解析轨迹(为什么落到这个 provider/model、auto 下是否换过渠道);后端 public_dict 空则省。 */
  trace?: string[]
}

export async function apiGetReceipts(limit = 20): Promise<AiReceipt[]> {
  const res = await fetch(`/api/ai/receipts?limit=${encodeURIComponent(String(limit))}`, { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-receipts ${res.status}`)
  const data = await res.json()
  return Array.isArray(data?.receipts) ? (data.receipts as AiReceipt[]) : []
}

export function useRecentReceipts(limit = 20): { receipts: AiReceipt[]; loaded: boolean; refresh: () => void } {
  const [receipts, setReceipts] = useState<AiReceipt[]>([])
  const [loaded, setLoaded] = useState(false)
  const refresh = useCallback(() => {
    apiGetReceipts(limit)
      .then((rs) => { setReceipts(rs); setLoaded(true) })
      .catch(() => setLoaded(true))
  }, [limit])
  useEffect(() => { refresh() }, [refresh])
  return { receipts, loaded, refresh }
}

// ── 助手 Turn(§7.1 协议存储读半)──────────────────────────────────────────────
// 消费 GET /api/ai/turns:断线重连恢复 + 「本轮 AI 改了什么」审计。列表态只出 turn 头;
// 传 turnId 拉全量(展开 events)。change_set 事件的 data.operation_ids 即 P0-D 整轮撤销的可逆单位。
export type AiTurnEvent = {
  event_id: string
  turn_id: string
  kind: string
  seq: number
  created_at: number
  data: Record<string, unknown>
}

export type AiTurnHead = {
  turn_id: string
  feature_id: string
  state: string
  created_at: number
  updated_at: number
  event_count: number
}

export type AiTurnFull = AiTurnHead & { actor_user_id: string; events: AiTurnEvent[] }

export async function apiGetRecentTurns(limit = 20): Promise<AiTurnHead[]> {
  const res = await fetch(`/api/ai/turns?limit=${encodeURIComponent(String(limit))}`, { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-turns ${res.status}`)
  const data = await res.json()
  return Array.isArray(data?.turns) ? (data.turns as AiTurnHead[]) : []
}

export async function apiGetTurn(turnId: string): Promise<AiTurnFull | null> {
  const res = await fetch(`/api/ai/turns?turn_id=${encodeURIComponent(turnId)}`, { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-turn ${res.status}`)
  const data = await res.json()
  return (data && typeof data.turn === 'object' && data.turn) ? (data.turn as AiTurnFull) : null
}

// ── 整轮撤销(§7.4 P0-D)──────────────────────────────────────────────────────
// 预览:列出本轮将撤销/冲突跳过/类型不支持/不可逆外部效果(「明确列出将撤销和不能撤销的部分」)。
// 执行:POST 逐个 dispatch 补偿正向命令(Form B),返回实际撤销/冲突/跳过。P0-C 变更卡的「整轮撤销」按钮用。
export type UndoRef = { op_id: string; command_id?: string; undoes?: string }
export type UndoSkip = { op_id: string; reason: string }
export type AiTurnUndoPreview = {
  will_revert: UndoRef[]
  conflict: UndoSkip[]
  skipped: UndoSkip[]
  irreversible: Array<Record<string, unknown>>
  entities: Array<{ entity_type: string; entity_id: string; fields: string[]; lifecycle: boolean }>
}
export type AiTurnUndoResult = {
  reverted: string[]
  conflict: UndoSkip[]
  skipped: UndoSkip[]
  dispatch_failed: UndoSkip[]
  irreversible: Array<Record<string, unknown>>
  project_generation?: number
}

export async function apiGetTurnUndoPreview(turnId: string): Promise<AiTurnUndoPreview> {
  const res = await fetch(`/api/ai/turn/${encodeURIComponent(turnId)}/undo-preview`, { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`undo-preview ${res.status}`)
  return res.json()
}

export async function apiUndoTurn(turnId: string): Promise<AiTurnUndoResult> {
  const res = await fetch(`/api/ai/turn/${encodeURIComponent(turnId)}/undo`, {
    method: 'POST', headers: { ...authHeader(), 'Content-Type': 'application/json' }, body: '{}',
  })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`undo-turn ${res.status}`)
  return res.json()
}

// ── §3.4 FeaturePreference:用户私有默认模型(设/清/列)────────────────────────────
// POST /api/ai/preference 设(空 model_id=清);GET /api/ai/preferences 列。resolver 的 preference
// 私有覆盖读它(pin>preference>intent>auto)。AI 设置中心 / 选择器「记住我的选择」调用。
export async function apiSetPreference(featureId: string, capability: string, modelId: string): Promise<void> {
  const res = await fetch('/api/ai/preference', {
    method: 'POST', headers: { ...authHeader(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ feature_id: featureId, capability, model_id: modelId }),
  })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-preference ${res.status}`)
}

export async function apiGetPreferences(): Promise<Record<string, string>> {
  const res = await fetch('/api/ai/preferences', { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-preferences ${res.status}`)
  const data = await res.json()
  return (data && typeof data.preferences === 'object' && data.preferences) || {}
}

// ── §3.4 CapabilityLane:用户全局能力泳道(设/清/列)────────────────────────────
// POST /api/ai/lane 覆盖整条泳道(空 models=清);GET /api/ai/lanes 列。resolver 五级优先第三层
// (pin>preference>lane>intent>auto)。AI 设置中心「能力优先序」调用。
export async function apiSetLane(capability: string, models: string[]): Promise<void> {
  const res = await fetch('/api/ai/lane', {
    method: 'POST', headers: { ...authHeader(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ capability, models }),
  })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-lane ${res.status}`)
}

export async function apiGetLanes(): Promise<Record<string, string[]>> {
  const res = await fetch('/api/ai/lanes', { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-lanes ${res.status}`)
  const data = await res.json()
  return (data && typeof data.lanes === 'object' && data.lanes) || {}
}
