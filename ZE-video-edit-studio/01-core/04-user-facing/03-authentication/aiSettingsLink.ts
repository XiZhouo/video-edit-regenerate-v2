/**
 * AI 设置的跨工具 deep-link 语义(规划 §5.1「提供跨工具稳定的 AI 设置 deep-link 语义」)。
 *
 * ## 缺口
 *
 * 「没绑就先说」这条链路本来是完整的:`/api/ai-routes` 已经按用户返回
 * `{available, reason, fix}`,`AiRouteFix` 里明明白白带着 `{action:'bind', provider, capability}`
 * 或 `{action:'configure', key}` —— 也就是**该去设置中心的哪一页、去干什么**,服务端早就算出来了。
 *
 * 但出口只有一个无参事件:`requestOpenCredentials()` 派发 `ze:open-credentials`。于是
 *
 *   - fix 里的 provider / capability / key **在门口被整个丢掉**,弹窗永远停在默认的「连接方式」页;
 *   - 事件是同一个页面内的,**跨工具根本走不通** —— 另一个 ZE 工具跑在别的端口上,
 *     没法往这个 app 里派发事件,而「工具 A 配置后工具 B 无需再次录入」正是 §5.1 的退出门。
 *
 * ## 形态
 *
 *   #ze/ai-settings?page=connect|caps|diag[&capability=…][&provider=…][&key=…]
 *
 * 选 hash 而不是路径:跨工具意味着链接由**另一个源**生成,hash 不需要目标工具的服务端配合路由,
 * 任何 ZE 工具的 webapp 都能直接消费。page 之外的三个参数不是装饰 —— 它们就是 `AiRouteFix`
 * 的字段,`targetFromRouteFix()` 负责把两者对上,所以链接的含义来自既有的 fix 结构,
 * 不是这里另起一套标签。
 *
 * ## 严格解析
 *
 * 认不出来就返回 null,不做「猜一个默认页」的兜底。deep-link 是**别人**生成的字符串:
 * 版本漂了、页名改了、被人手改了,静默落到默认页只会让用户对着一个不相干的页面发懵,
 * 而调用方连「这条链接没生效」都不知道。返回 null 让调用方能选择照常打开或提示。
 */

/** 设置中心的页 —— 与 CopilotAccountDialog 的 tab 一一对应。 */
export const AI_SETTINGS_PAGES = ['connect', 'caps', 'diag'] as const
export type AiSettingsPage = (typeof AI_SETTINGS_PAGES)[number]

export type AiSettingsTarget = {
  page: AiSettingsPage
  /** 缺哪个能力(来自 AiRouteFix.capability),用于在目标页高亮/预选。 */
  capability?: string
  /** 该绑哪个 provider(来自 AiRouteFix.provider)。 */
  provider?: string
  /** 该配哪个配置项(来自 AiRouteFix.configure.key)。 */
  key?: string
}

/** hash 的固定前缀。带 `ze/` 命名空间,避免和工具自己的锚点(见 GalleryNav 的 `#<id>`)撞车。 */
export const AI_SETTINGS_HASH_ROUTE = 'ze/ai-settings'

function isPage(value: string): value is AiSettingsPage {
  return (AI_SETTINGS_PAGES as readonly string[]).includes(value)
}

/** 生成 deep-link 的 hash 段(含前导 `#`)。 */
export function buildAiSettingsHash(target: AiSettingsTarget): string {
  const params = new URLSearchParams({ page: target.page })
  // 只写有值的,免得链接里出现一串 `&provider=` 空参数
  if (target.capability) params.set('capability', target.capability)
  if (target.provider) params.set('provider', target.provider)
  if (target.key) params.set('key', target.key)
  return `#${AI_SETTINGS_HASH_ROUTE}?${params.toString()}`
}

/**
 * 解析 deep-link。不是本路由、或 page 不认识,一律返回 null。
 *
 * 容忍前导 `#` 的有无(`location.hash` 带,手工拼的常常不带)。
 */
export function parseAiSettingsHash(hash: string | null | undefined): AiSettingsTarget | null {
  if (!hash) return null
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const sep = raw.indexOf('?')
  if (sep < 0) return null
  if (raw.slice(0, sep) !== AI_SETTINGS_HASH_ROUTE) return null

  const params = new URLSearchParams(raw.slice(sep + 1))
  const page = params.get('page')
  if (!page || !isPage(page)) return null

  const target: AiSettingsTarget = { page }
  for (const field of ['capability', 'provider', 'key'] as const) {
    const value = params.get(field)
    if (value) target[field] = value
  }
  return target
}

/** 供另一个工具生成完整链接:`aiSettingsUrl('https://host:8104', {page:'caps'})`。 */
export function aiSettingsUrl(origin: string, target: AiSettingsTarget): string {
  return `${origin.replace(/\/+$/, '')}/${buildAiSettingsHash(target)}`
}

/**
 * 把 `/api/ai-routes` 给出的 fix 翻译成落点 —— 服务端已经判断过「该去干什么」,
 * 这里只做结构映射,不重新判断。
 *
 * - `bind`      → 连接方式页,带上该绑的 provider 和缺的 capability
 * - `configure` → 能力与默认项页,带上该配的 key
 * - 没有 fix    → 连接方式页(设置中心的默认落点)
 */
export function targetFromRouteFix(
  fix: { action: 'bind'; provider: string; capability?: string }
     | { action: 'configure'; key: string }
     | null
     | undefined,
): AiSettingsTarget {
  if (fix?.action === 'bind') {
    const target: AiSettingsTarget = { page: 'connect', provider: fix.provider }
    if (fix.capability) target.capability = fix.capability
    return target
  }
  if (fix?.action === 'configure') return { page: 'caps', key: fix.key }
  return { page: 'connect' }
}

/**
 * 判断一个值是不是合法落点。
 *
 * 存在的理由很具体:`requestOpenCredentials` 此前是无参函数,调用点写的是
 * `onClick={requestOpenCredentials}` —— React 会把 MouseEvent 当第一个实参传进去。
 * 给它加可选参数的那一刻,MouseEvent 就会被当成「落点」。调用点已改成显式传参,
 * 但这道守卫留着:漏改一处不该表现为跳到一个乱七八糟的页面。
 */
export function isAiSettingsTarget(value: unknown): value is AiSettingsTarget {
  if (!value || typeof value !== 'object') return false
  const page = (value as { page?: unknown }).page
  return typeof page === 'string' && isPage(page)
}
