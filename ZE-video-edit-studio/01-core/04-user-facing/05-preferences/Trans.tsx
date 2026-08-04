/**
 * Trans — 渲染含 inline 富文本标记的翻译字串(避免字典 hardcode JSX 或拆 key 爆炸)。
 *
 * 两种标记:`**bold**` → <b>;`<kbd>⌘↵</kbd>` → 键帽。普通 t() 不解析标记。
 *
 * 视觉单一真相源:本组件**不内联视觉**。输出包一层 `.ze-rich`(02-atoms/text.css),
 * 由其后代选择器给 `<b>` 固定主墨(脱离上下文次要色而突出);`<kbd>` 贴 `.ze-kbd` 键帽原子。
 * JSX 只发 className。由 css_layer_guard 规则 8 LAYER_INLINE_VISUAL 强制。
 */

import { type ReactNode } from 'react'

import { type TKey } from '@ze/i18n'

import { useT } from './useT'

export function Trans({ k, params }: { k: TKey; params?: Record<string, string | number> }): ReactNode {
  const t = useT()
  const raw = t(k, params)
  const parts = raw.split(/(\*\*[^*]+\*\*|<kbd>[^<]+<\/kbd>)/)
  return (
    <span className="ze-rich">
      {parts.map((p, i) => {
        if (p.startsWith('**') && p.endsWith('**')) {
          return <b key={i}>{p.slice(2, -2)}</b>
        }
        if (p.startsWith('<kbd>') && p.endsWith('</kbd>')) {
          return <kbd key={i} className="ze-kbd">{p.slice(5, -6)}</kbd>
        }
        return <span key={i}>{p}</span>
      })}
    </span>
  )
}
