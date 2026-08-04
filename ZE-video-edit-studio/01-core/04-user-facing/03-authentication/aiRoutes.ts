/**
 * AI 通道可用性(3.56「没绑就先说」不变量的前端数据源)。
 *
 * 消费 GET /api/ai-routes:按当前用户返回每条生图路线 {route, available, reason, fix}。
 * 配套 useAiRoutes() hook 与 AiRouteGate 门控组件(01-layout/AiRouteGate.tsx):
 * 声明了 requires_route 的命令入口,在用户点击之前就呈现通道状态——
 * 不可用 = 置灰 + 原因 + 一键去绑定,绝不让人点了失败再自己猜。
 */
import { useCallback, useEffect, useState } from 'react'
import { authHeader, triggerAuthError } from './authApi'
import { isAiSettingsTarget, type AiSettingsTarget } from './aiSettingsLink'

export type AiRouteReason =
  | 'not_bound' | 'credential_stale' | 'service_unreachable'
  | 'region_blocked' | 'quota_low' | 'not_configured'

export type AiRouteFix =
  | { action: 'bind'; provider: string; capability?: string }
  | { action: 'configure'; key: string }

export type AiRoute = {
  // 'auto' = 服务端 ze.image.route='auto' 时补的元路线行(可用性=内网服务已配 或 已绑任一个人路线),
  // 供 AiRouteGate 在 current='auto' 时也能预呈现状态,不被 !info 分支旁路。
  route: 'auto' | 'service' | 'codex-cli' | 'openai-api' | 'gemini-api'
  available: boolean
  reason: AiRouteReason | null
  fix: AiRouteFix | null
}

export type AiRoutesState = {
  routes: AiRoute[]
  current: string
  loaded: boolean
  refresh: () => void
}

export async function apiGetAiRoutes(): Promise<{ routes: AiRoute[]; current: string }> {
  const res = await fetch('/api/ai-routes', { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-routes ${res.status}`)
  return res.json()
}

/** 打开「我的 AI 账号」弹窗的全局事件(SettingsMenu 侧监听;AiRouteGate 的一键修复出口)。 */
export const OPEN_CREDENTIALS_EVENT = 'ze:open-credentials'
/** 当前用户的 AI 账号发生绑定、切换默认项或删除时广播，供所有能力入口立即重查。 */
export const CREDENTIALS_CHANGED_EVENT = 'ze:credentials-changed'

/**
 * 打开设置中心,可指定落点。
 *
 * 落点从 `AiRouteFix` 来(见 aiSettingsLink.targetFromRouteFix):服务端既然已经算出
 * 「该绑哪个 provider / 该配哪个 key」,就不该在门口把这些丢掉、让弹窗永远停在默认页。
 * 非法值一律当没传 —— 调用点曾是 `onClick={requestOpenCredentials}` 这种写法,
 * React 会把 MouseEvent 当实参传进来。
 */
export function requestOpenCredentials(target?: AiSettingsTarget): void {
  const detail = isAiSettingsTarget(target) ? target : undefined
  window.dispatchEvent(new CustomEvent(OPEN_CREDENTIALS_EVENT, { detail }))
}

export function notifyCredentialsChanged(target: EventTarget = window): void {
  target.dispatchEvent(new Event(CREDENTIALS_CHANGED_EVENT))
}

export function useAiRoutes(): AiRoutesState {
  const [routes, setRoutes] = useState<AiRoute[]>([])
  const [current, setCurrent] = useState('service')
  const [loaded, setLoaded] = useState(false)

  const refresh = useCallback(() => {
    apiGetAiRoutes()
      .then((d) => { setRoutes(d.routes); setCurrent(d.current); setLoaded(true) })
      .catch(() => setLoaded(true))   // 拿不到时按钮不硬锁(驱动层 428 仍兜底)
  }, [])

  useEffect(() => { refresh() }, [refresh])
  return { routes, current, loaded, refresh }
}
