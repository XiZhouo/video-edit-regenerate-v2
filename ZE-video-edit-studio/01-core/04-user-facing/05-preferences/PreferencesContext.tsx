/**
 * PreferencesContext — 用户偏好(主题 + 语言)的 React glue。
 *
 * 偏好层 = theme + locale,同一类东西(per-user / per-device / localStorage / 即时)。本 Provider:
 *   - theme state(localStorage `ze.theme`)→ effect 写 `<html data-theme>`(tokens.css 的
 *     `[data-theme="dark"]` 接管);`system` 经 matchMedia 解析并随 OS 切换。
 *   - locale 来自 05-i18n 的模块级 store(localStorage `ze.locale`)→ effect 写 `<html lang>`。
 *
 * 词表/resolver 在 `@ze/i18n`(纯 TS 层);本文件只做 React 状态 + DOM 应用(穿 class 的在 core)。
 *
 * 修复:此前 theme/locale 经 header dispatch 打到后端 /api/command(无此命令,静默 no-op)→
 * 夜间模式/语言其实不生效。改为本 Provider 直接应用 + 持久化,header 走 onChange 回调。
 */

import {
  createContext, useCallback, useContext, useEffect, useState, useSyncExternalStore,
  type ReactNode,
} from 'react'

import { getLocale, setLocale as storeSetLocale, subscribeLocale, LOCALE_META, type Locale } from '@ze/i18n'

export type Theme = 'light' | 'dark' | 'system'

const THEME_KEY = 'ze.theme'

function initialTheme(): Theme {
  try {
    const s = localStorage.getItem(THEME_KEY)
    if (s === 'light' || s === 'dark' || s === 'system') return s
  } catch {
    /* localStorage 不可用 */
  }
  return 'system'
}

function resolveTheme(t: Theme): 'light' | 'dark' {
  if (t === 'system') {
    return typeof matchMedia !== 'undefined' && matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light'
  }
  return t
}

type PreferencesValue = {
  theme: Theme
  setTheme: (t: Theme) => void
  locale: Locale
  setLocale: (l: Locale) => void
}

const PreferencesContext = createContext<PreferencesValue | null>(null)

export function PreferencesProvider({ children }: { children: ReactNode }): ReactNode {
  const [theme, setThemeState] = useState<Theme>(initialTheme)
  // locale 订 05-i18n 模块级 store(getT 非组件读同一源)
  const locale = useSyncExternalStore(subscribeLocale, getLocale, getLocale)

  // 应用 <html data-theme>;system 跟随 OS 偏好实时变
  useEffect(() => {
    const apply = () => {
      document.documentElement.setAttribute('data-theme', resolveTheme(theme))
    }
    apply()
    if (theme === 'system' && typeof matchMedia !== 'undefined') {
      const mq = matchMedia('(prefers-color-scheme: dark)')
      mq.addEventListener('change', apply)
      return () => mq.removeEventListener('change', apply)
    }
    return undefined
  }, [theme])

  // 应用 <html lang>(BCP-47 标签从语言注册表取,增减语言无需改这里)
  useEffect(() => {
    document.documentElement.lang = LOCALE_META[locale].htmlLang
  }, [locale])

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t)
    try {
      localStorage.setItem(THEME_KEY, t)
    } catch {
      /* localStorage 不可用 */
    }
  }, [])

  const setLocale = useCallback((l: Locale) => storeSetLocale(l), [])

  return (
    <PreferencesContext.Provider value={{ theme, setTheme, locale, setLocale }}>
      {children}
    </PreferencesContext.Provider>
  )
}

export function usePreferences(): PreferencesValue {
  const v = useContext(PreferencesContext)
  if (!v) throw new Error('usePreferences must be used within <PreferencesProvider>')
  return v
}

export function useTheme(): { theme: Theme; setTheme: (t: Theme) => void } {
  const { theme, setTheme } = usePreferences()
  return { theme, setTheme }
}

export function useLocale(): { locale: Locale; setLocale: (l: Locale) => void } {
  const { locale, setLocale } = usePreferences()
  return { locale, setLocale }
}
