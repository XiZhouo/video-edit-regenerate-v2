/**
 * 05-i18n — "说"的词表层(纯 TS,无 React;03-css-rules"看" / 04-ops"配" 的孪生)。
 *
 * 暴露:类型化字典(zh 真源 + typed en 镜像)+ key 字面量联合(I18nKey)+ 非 React 翻译
 * (translate / getT)。React 侧(useT / Trans / <html lang> 应用 / theme)在 core 的
 * 04-user-facing/05-preferences/ —— 正如 css 是纯 .css、穿 class 的组件在 core。
 *
 * 业务字串:mod 经 `setModMessages(modZh, modEn)` 合并到 `mod.*` 命名空间(App 启动注入)。
 * 框架 key(I18nKey)有静态补全 + 错 key 编译失败;mod key 是运行时 string(作者自管)。
 */

import { getLocale, type Locale } from './locale-store'
import { LOCALE_CODES, LOCALE_META, DEFAULT_LOCALE } from './locales'
import { zh } from './zh'

export type { Locale }
export { LOCALE_CODES, DEFAULT_LOCALE, LOCALE_META } from './locales'
export { getLocale, setLocale, subscribeLocale } from './locale-store'

// Stringify<T>:保留 T 的 shape,把所有 leaf string literal 放宽为普通 string。
// Dict 是"结构约束"(每 leaf 必须有、且是 string),不锁字面量值 → en 可写英文。
type Stringify<T> = T extends string
  ? string
  : { [K in keyof T]: Stringify<T[K]> }

export type Dict = Stringify<typeof zh>

// 递归把所有 leaf 路径展开成字面量联合,t() 第一参数自动补全 + 错 key 编译失败。
type Leaves<T, P extends string = ''> = {
  [K in keyof T & string]: T[K] extends string
    ? `${P}${K}`
    : T[K] extends object
      ? Leaves<T[K], `${P}${K}.`>
      : never
}[keyof T & string]

export type I18nKey = Leaves<typeof zh>
// 框架 key 有补全;mod key(运行时合并)是任意 string —— `string & {}` 保留 I18nKey 的字面量补全。
export type TKey = I18nKey | (string & {})

// 框架词表基底(不含 mod),从注册表派生 —— 增减语言只动 locales.ts,这里自动跟随。
const BASE: Record<string, Record<string, unknown>> = Object.fromEntries(
  LOCALE_CODES.map((c) => [c, LOCALE_META[c].dict]),
)

// DICTS 可变副本:setModMessages 把 mod 字典并到各语言的 `mod.*`。
let DICTS: Record<string, Record<string, unknown>> = Object.fromEntries(
  LOCALE_CODES.map((c) => [c, { ...BASE[c] }]),
)

/**
 * App 启动注入 mod 业务字典(`mod.*` 命名空间),按 locale-map 给每门语言合并。
 * 某 locale 缺译 → 回落 DEFAULT_LOCALE 的 mod(框架已加新语言、该 skill 还没补 mod 译文时不崩)。
 */
export function setModMessages(
  byLocale: Partial<Record<Locale, Record<string, unknown>>>,
): void {
  const fallback = byLocale[DEFAULT_LOCALE]
  if (fallback === undefined
      && typeof import.meta !== 'undefined' && (import.meta as { env?: { DEV?: boolean } }).env?.DEV) {
    console.warn(`[i18n] setModMessages: 缺默认 locale '${DEFAULT_LOCALE}' 的 mod 词表,所有 mod.* 将回落为 key 串`)
  }
  DICTS = Object.fromEntries(
    LOCALE_CODES.map((c) => [c, { ...BASE[c], mod: byLocale[c] ?? fallback ?? {} }]),
  )
}

function resolve(dict: Record<string, unknown>, key: string): string {
  let cur: unknown = dict
  for (const seg of key.split('.')) {
    if (cur && typeof cur === 'object' && seg in (cur as object)) {
      cur = (cur as Record<string, unknown>)[seg]
    } else {
      // 缺 key:DEV 抱怨,生产静默回落 key 字符串(greppable)
      if (typeof import.meta !== 'undefined' && (import.meta as { env?: { DEV?: boolean } }).env?.DEV) {
        console.warn(`[i18n] missing key: ${key}`)
      }
      return key
    }
  }
  return typeof cur === 'string' ? cur : key
}

function interpolate(template: string, params?: Record<string, string | number>): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (_, k) => (k in params ? String(params[k]) : `{${k}}`))
}

/** 纯函数翻译(给定 locale)。 */
export function translate(locale: Locale, key: TKey, params?: Record<string, string | number>): string {
  return interpolate(resolve(DICTS[locale], key), params)
}

/** 非组件代码(service / event handler):一次性读当前 locale,不订阅。 */
export function getT() {
  const locale = getLocale()
  return (k: TKey, p?: Record<string, string | number>) => translate(locale, k, p)
}
