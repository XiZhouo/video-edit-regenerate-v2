/**
 * detect — 首次加载解析 locale:localStorage 有就用,否则默认 zh。
 *
 * 不走 navigator.language —— ZE 是内部工具,主用户中文(同 sketch-to-design 决策)。
 */

import { LOCALE_CODES, DEFAULT_LOCALE, type Locale } from './locales'
import { LOCALE_STORAGE_KEY } from './constants'

export function resolveInitialLocale(): Locale {
  try {
    const stored = localStorage.getItem(LOCALE_STORAGE_KEY)
    if (stored !== null && (LOCALE_CODES as readonly string[]).includes(stored)) {
      return stored as Locale
    }
  } catch {
    /* localStorage 不可用 */
  }
  return DEFAULT_LOCALE
}
