/**
 * 05-preferences — 偏好层 React glue 桶导出(主题 + 语言 + 个人视图状态)。
 *
 * 词表/resolver 在 `@ze/i18n`(纯 TS 层);本目录是穿戴它的 core React 侧。
 * 组件统一从这里取:`import { useT, Trans, useTheme, useLocale } from '@ze/core/04-user-facing/05-preferences'`。
 */

export {
  PreferencesProvider, usePreferences, useTheme, useLocale, type Theme,
} from './PreferencesContext'
export { useT } from './useT'
export { Trans } from './Trans'
export {
  VIEW_STATE_SCHEMA, VIEW_STATE_ENVELOPE_VERSION,
  isViewStateScope, viewStateStorageKey, readViewStateSlice, writeViewStateSlice, removeViewStateSlice,
  useScopedViewState,
  type ViewStateScope, type ViewStateStorage, type ViewStateSliceCodec,
  type ViewStateReadResult, type ScopedViewState, type ScopedViewStateSetter,
  type UseScopedViewStateOptions,
} from './viewStateStore'
export {
  DISCLOSURE_VIEW_STATE_SLICE,
  disclosureStateKey, disclosureViewStateCodec,
  useScopedDisclosureGroup, DisclosureStateProvider, useDisclosureItemState,
  type DisclosureViewState, type UseScopedDisclosureGroupOptions,
  type ScopedDisclosureGroup, type DisclosureStateProviderProps, type DisclosureItemState,
} from './disclosureState'

// 便利:i18n 层的非 React API 也从此再导出(App 注入 mod 字典、handler 用 getT)
export { getT, setModMessages, type Locale, type I18nKey, type TKey } from '@ze/i18n'
