/**
 * LayoutTree 类型 — Main 区二叉分割树 (父级 layout 叶子,SPEC §2.2)
 *
 * 三种节点:EditorSlot(一格 editor) / Split(二叉分屏) / TabContainer(切 tab)。
 * 约束:JSON 可序列化;Split 永远二叉(三栏 = 嵌套 Split);TabContainer.children
 * 可为任意节点(tab 内可再 split);无循环引用。每节点带 id(React key + op 定位)。
 *
 * LayoutTree 是单机状态:不进 Document / OpLog,不参与协作同步(SPEC §2.5)。
 * editor_class 的实装组件由 03-editor 的 EDITOR_CLASSES 注册表提供。
 */

import type { EditorClass, ZoneOverrides } from './03-editor'

export type { EditorClass } from './03-editor'

export type NodeId = string
/** entity_type 名;index 0 为主绑定 */
export type EntityBinding = string

export interface EditorSlotNode {
  kind: 'slot'
  id: NodeId
  editor_class: EditorClass
  entity_bindings: EntityBinding[]
  /** 编辑器名(横栏左侧标题)。mod 声明这个槽是干什么的编辑器(如「2D 标注」「生成管线」);
   *  缺省时编辑器各自回退(实体名/类名)。规则:横栏左 = 编辑器名,不乱抓实体名/管线 id。 */
  name?: string
  /** 可译编辑器名;优先于 name。 */
  nameKey?: string
  /** editor 局部状态(zoom/scroll/selection);M2 暂不持久化进 tree */
  view_state?: Record<string, unknown>
  /** editor 内 zone 的可见性 / 拓展点 override(mod 经 layout 声明;装配缝 slotAdapters 消费)。 */
  zone_overrides?: ZoneOverrides
}

export interface SplitNode {
  kind: 'split'
  id: NodeId
  direction: 'horizontal' | 'vertical'
  /** 0..1,first/top 节点占比 */
  ratio: number
  first: LayoutNode
  second: LayoutNode
  /** 作为 tab 直接子节点时的可译标题(i18n key),优先于 name;缺省时 tab 头回退 'Split'。
   *  规则同 EditorSlotNode.nameKey:mod 经 04-layout 声明这个 tab 页是什么(如「五编辑器」「图层工作台」)。 */
  nameKey?: string
  /** 作为 tab 直接子节点时的语言中立回退标题;nameKey 优先。 */
  name?: string
}

export interface TabsNode {
  kind: 'tabs'
  id: NodeId
  children: LayoutNode[]
  active_index: number
  /** 作为(嵌套)tab 直接子节点时的可译标题(i18n key),优先于 name;缺省时 tab 头回退 'Tabs'。 */
  nameKey?: string
  /** 作为(嵌套)tab 直接子节点时的语言中立回退标题;nameKey 优先。 */
  name?: string
}

export type LayoutNode = EditorSlotNode | SplitNode | TabsNode

/** EditorSlot 最小尺寸(SPEC §2.4):宽 240 / 高 160 */
export const MIN_SLOT_W = 240
export const MIN_SLOT_H = 160

export const isSlot = (n: LayoutNode): n is EditorSlotNode => n.kind === 'slot'
export const isSplit = (n: LayoutNode): n is SplitNode => n.kind === 'split'
export const isTabs = (n: LayoutNode): n is TabsNode => n.kind === 'tabs'
