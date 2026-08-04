/**
 * useT — React 翻译 hook(订 05-i18n 模块级 locale store → locale 变即重渲染)。
 *
 * 纯 React glue:translate 来自 `@ze/i18n`(纯 TS 层)。组件:`const t = useT(); t('common.save')`。
 * 非组件代码用 `@ze/i18n` 的 getT()。
 */

import { useCallback, useSyncExternalStore } from 'react'

import { getLocale, subscribeLocale, translate, type TKey } from '@ze/i18n'

export function useT(): (k: TKey, p?: Record<string, string | number>) => string {
  const locale = useSyncExternalStore(subscribeLocale, getLocale, getLocale)
  // 稳定引用:仅 locale 变时换新(避免传 t 作 prop / deps 的组件无谓重渲染)
  return useCallback(
    (k: TKey, p?: Record<string, string | number>) => translate(locale, k, p),
    [locale],
  )
}
