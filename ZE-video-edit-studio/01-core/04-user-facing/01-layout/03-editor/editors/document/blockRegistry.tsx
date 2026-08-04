/**
 * blockRegistry — 块类型 → 展示态 HTML 片段(document 编辑器消费)。
 *
 * 「块 = L2 atom」:每种块的视觉只住 L2(.ze-text[data-prose] 经后代选择器排版 markdown 注入的
 * h/p/列表/引用/码/hr/链),本注册表只把块实体映射成对应的语义裸标签 HTML,塞进 prose 容器。
 * 行内标记复用零依赖 markdown 渲染器(lib/markdown),不引重库。
 *
 * M0:paragraph / heading(level) / divider,未知类型回退按段落渲文本。
 * M1 升级为开放集组件注册表(callout/code/checklist/figure/toggle + mod 经 view_state.blockRenderers 注入)。
 */

import { renderInlineMarks, renderMarkdown } from '../../lib/markdown'
import type { EntityDict } from '../../types'

/** 块字段映射(闭集;由 view_state 声明,缺省见 document binding)。 */
export type BlockFieldMap = {
  blockTypeField: string
  textField: string
  mediaField: string
}

/** 取块类型(缺省 paragraph)。 */
export function blockTypeOf(e: EntityDict, f: BlockFieldMap): string {
  const v = e.data[f.blockTypeField]
  return typeof v === 'string' && v ? v : 'paragraph'
}

/** 块是否承载可内联编辑的文本(divider 等无文本块 false → 点击只选不进编辑)。 */
export function blockIsTextEditable(blockType: string): boolean {
  return blockType !== 'divider'
}

function textOf(e: EntityDict, f: BlockFieldMap): string {
  const v = e.data[f.textField]
  return typeof v === 'string' ? v : ''
}

/** 块类型 → 渲染函数(返回塞进 .ze-text[data-prose] 的 HTML;M0 内置集,M1 转开放注册表)。 */
const BUILTIN: Record<string, (e: EntityDict, f: BlockFieldMap) => string> = {
  divider: () => '<hr/>',
  heading: (e, f) => {
    const raw = e.data.level
    const lvl = typeof raw === 'number' && raw >= 1 && raw <= 6 ? raw : 2
    return `<h${lvl}>${renderInlineMarks(textOf(e, f))}</h${lvl}>`
  },
  paragraph: (e, f) => `<p>${renderInlineMarks(textOf(e, f))}</p>`,
  // markdown:整段块级 markdown(标题/列表/任务清单/引用/围栏码/分隔/段落都渲)——
  // 百科稿一节 = 一块的粗粒度模型;聚焦编辑显源、失焦渲染。
  markdown: (e, f) => renderMarkdown(textOf(e, f)),
}

/** 渲染一个块的展示态 HTML 片段(失焦视图)。未知 blockType 回退按段落渲。 */
export function renderBlockHtml(e: EntityDict, f: BlockFieldMap): string {
  const fn = BUILTIN[blockTypeOf(e, f)] ?? BUILTIN.paragraph
  return fn(e, f)
}
