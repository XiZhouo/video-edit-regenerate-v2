/**
 * tabLabel — TabsNode header 里一个 tab 的标题解析(纯函数;抽出 LayoutRenderer 便于 vitest)。
 *
 * 三种节点统一优先级:nameKey(经 t 译) → name(语言中立) → kind 兜底。
 *   - slot 兜底 = `editor_class·primary_binding`(缺绑定则 editor_class)
 *   - split / tabs 兜底 = 'Split' / 'Tabs'
 *
 * 加名字段前,child 是 split 时固定显示 'Split'(build-v3 demo 与所有多 tab 下游 skill——如
 * ZE-open-design-studio 三个 tab 全显 'Split'——皆受影响)。现 mod 可在 04-layout 给 tab-root
 * 的 split / tabs 声明 nameKey(或 name),tabLabel 优先消费,slot 亦优先其自身 nameKey/name。
 *
 * t 形参用最宽松的 `(key: string) => string`:React 侧传 useT()(locale 变即重渲染),
 * 非 React 调用可传 getT();test 传假译器。
 */
import type { LayoutNode } from '../types'

export function tabLabel(n: LayoutNode, t: (key: string) => string): string {
  if (n.nameKey) return t(n.nameKey)
  if (n.name) return n.name
  if (n.kind === 'slot') {
    return n.entity_bindings[0] ? `${n.editor_class}·${n.entity_bindings[0]}` : n.editor_class
  }
  return n.kind === 'split' ? 'Split' : 'Tabs'
}
