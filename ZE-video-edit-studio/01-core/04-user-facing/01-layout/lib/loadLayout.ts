/**
 * loadLayout — 把 mod 的布局声明(02-mod/04-layout)翻成 LayoutNode。
 *
 * 优先级:preset+slots → PRESETS[preset](slots);否则 raw tree;否则 coreDefaultTree()。
 * 让 mod 用 ~6 行声明(preset + 绑定)就出一棵可序列化布局树,App.tsx 不再内联布局
 * (Block3 把 App.tsx 的 DEMO_TREE 泄漏挪进 mod 后,App 调本函数读 mod 声明)。
 */

import { PRESETS, type PresetName, type SlotSpec } from './presets'
import type { EditorClass, LayoutNode } from '../types'

export type ModLayoutConfig = {
  preset?: PresetName
  /** preset 的具名 slot → SlotSpec(如 catalog_browse 的 list/workbench/inspector)*/
  slots?: Record<string, SlotSpec>
  /** 逃生舱:直接给一棵 LayoutNode(preset 不够用时)*/
  tree?: LayoutNode
}

/** 兜底布局:单 slot table 绑给定 entity_type(无则空绑,渲染"未绑定"提示)。 */
function fallbackTree(entityType?: string): LayoutNode {
  return {
    kind: 'slot', id: 'core-default',
    editor_class: 'table' as EditorClass,
    entity_bindings: entityType ? [entityType] : [],
  }
}

export function loadLayout(config?: ModLayoutConfig, fallbackEntityType?: string): LayoutNode {
  if (config?.preset && config.slots) {
    const build = PRESETS[config.preset] as ((slots: Record<string, SlotSpec>) => LayoutNode) | undefined
    if (build) {
      try {
        return build(config.slots)
      } catch {
        /* slots 不匹配 preset 形状 → 落兜底 */
      }
    }
  }
  if (config?.tree) return config.tree
  return fallbackTree(fallbackEntityType)
}
