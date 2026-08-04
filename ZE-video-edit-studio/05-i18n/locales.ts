/**
 * locales.ts — 支持语言集的**唯一真源**(注册表)。
 *
 * 增减语言只动这里 + 加/删对应词表文件,其余全部派生:
 *   - `Locale` 类型(字面量联合,支撑 t() 补全与编译期校验)
 *   - index.ts 的 DICTS(运行时词表表)
 *   - detect.ts 的 locale 校验
 *   - PreferencesContext 的 <html lang>
 *   - SettingsMenu 的语言切换项
 *
 * 加日语示例:① LOCALE_CODES 加 'ja' ② LOCALE_META 加 ja 项 ③ 建 ja.ts(`as Dict`,缺 key 编译失败)。
 * 本文件属 05-i18n 同步层 —— 改它 + bump i18n interface_version,Skill.Sync 全舰队下发。
 */

import { zh } from './zh'
import { en } from './en'

// 码数组:字面量 `as const` → Locale 联合类型从它推导。
export const LOCALE_CODES = ['zh', 'en'] as const
export type Locale = (typeof LOCALE_CODES)[number]

// 默认 / 回落语言(detect 无命中、mod 缺译时落到它)。
export const DEFAULT_LOCALE: Locale = 'zh'

// 每门语言的元数据:<html lang> 用的 BCP-47 标签 + 原生名(各 UI 语言下都按原文展示,故属注册表元数据而非译文)+ 框架词表。
export const LOCALE_META: Record<Locale, { htmlLang: string; label: string; dict: Record<string, unknown> }> = {
  zh: { htmlLang: 'zh-CN', label: '中文', dict: zh },
  en: { htmlLang: 'en', label: 'English', dict: en },
}
