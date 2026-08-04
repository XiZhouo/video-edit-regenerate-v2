/**
 * locale-store — 模块级 locale 观察者(纯 TS,无 React;替代 sketch-to-design 的 Zustand)。
 *
 * 05-i18n 是"说"的词表层(03-css-rules"看" / 04-ops"配" 的孪生),**不含 React**。
 * 本 store 只管:当前 locale 状态 + localStorage 持久化 + 订阅通知。
 * React 侧(useT / useLocale / <html lang> 应用)在 core 的 04-user-facing/05-preferences/。
 *
 * 非组件代码(service / event handler)用 getLocale() / getT() 直接读;React 组件经 core 的
 * useT() 走 useSyncExternalStore 订阅 subscribeLocale。
 */

import { resolveInitialLocale } from './detect'
import { LOCALE_STORAGE_KEY } from './constants'
import type { Locale } from './locales'

export type { Locale }

let _locale: Locale = resolveInitialLocale()
const listeners = new Set<() => void>()

export function getLocale(): Locale {
  return _locale
}

export function subscribeLocale(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

export function setLocale(next: Locale): void {
  if (next === _locale) return
  _locale = next
  try {
    localStorage.setItem(LOCALE_STORAGE_KEY, next)
  } catch {
    /* localStorage 不可用(隐私模式 / SSR) */
  }
  listeners.forEach((fn) => fn())
}
