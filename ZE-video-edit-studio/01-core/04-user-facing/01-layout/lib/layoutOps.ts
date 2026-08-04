/**
 * layoutOps — LayoutTree 不可变操作(纯函数、递归、结构共享)
 *
 * 只重建从根到目标节点的路径,其余子树原 ref 复用(immutable update,单一数据源)。
 * 操作集 = setRatio / setActiveTab / moveTab(容器内重排)+ 只读查询(SPEC §2.3)。
 * 〔运行时增删槽位算子 splitSlot/closeNode 已按用户裁决移除(2026-06-13):
 *   槽位增删由 mod default layout 静态声明。〕
 */

import type { LayoutNode, NodeId } from '../types'

/** 在 tree 里找到 nodeId,用 updater 替换它,返回新 tree(未命中则结构不变)。 */
export function updateNode(
  tree: LayoutNode,
  nodeId: NodeId,
  updater: (n: LayoutNode) => LayoutNode,
): LayoutNode {
  if (tree.id === nodeId) return updater(tree)

  switch (tree.kind) {
    case 'split': {
      const first = updateNode(tree.first, nodeId, updater)
      const second = updateNode(tree.second, nodeId, updater)
      if (first === tree.first && second === tree.second) return tree
      return { ...tree, first, second }
    }
    case 'tabs': {
      let changed = false
      const children = tree.children.map((c) => {
        const n = updateNode(c, nodeId, updater)
        if (n !== c) changed = true
        return n
      })
      return changed ? { ...tree, children } : tree
    }
    case 'slot':
      return tree
  }
}
/** 改 Split.ratio(clamp 0.02..0.98,避免某栏宽 0)。 */
export function setRatio(tree: LayoutNode, splitId: NodeId, ratio: number): LayoutNode {
  const clamped = Math.min(0.98, Math.max(0.02, ratio))
  return updateNode(tree, splitId, (n) => (n.kind === 'split' ? { ...n, ratio: clamped } : n))
}

/** 改 TabContainer.active_index(clamp 到 [0, len-1])。 */
export function setActiveTab(tree: LayoutNode, tabsId: NodeId, index: number): LayoutNode {
  return updateNode(tree, tabsId, (n) => {
    if (n.kind !== 'tabs') return n
    const i = Math.min(Math.max(index, 0), n.children.length - 1)
    return { ...n, active_index: i }
  })
}

/** 按 id 找节点(只读)。 */
export function findNode(tree: LayoutNode, nodeId: NodeId): LayoutNode | null {
  if (tree.id === nodeId) return tree
  if (tree.kind === 'split') return findNode(tree.first, nodeId) ?? findNode(tree.second, nodeId)
  if (tree.kind === 'tabs') {
    for (const c of tree.children) {
      const f = findNode(c, nodeId)
      if (f) return f
    }
  }
  return null
}

/**
 * 收集全树节点 id(先序)。供持久化恢复校验:用户能改的只有 ratio / active_index,
 * 不能增删节点 —— 持久化树的 id 集 ≠ 当前 default 树 → 弃用回 default(SPEC §2.5)。
 */
export function collectNodeIds(tree: LayoutNode, out: NodeId[] = []): NodeId[] {
  out.push(tree.id)
  if (tree.kind === 'split') {
    collectNodeIds(tree.first, out)
    collectNodeIds(tree.second, out)
  } else if (tree.kind === 'tabs') {
    for (const c of tree.children) collectNodeIds(c, out)
  }
  return out
}

/** 两棵树的节点 id 集是否完全一致(与顺序无关;重复 id 视为不一致——id 必须全树唯一)。 */
export function sameNodeIdSet(a: LayoutNode, b: LayoutNode): boolean {
  const ia = collectNodeIds(a)
  const ib = collectNodeIds(b)
  if (ia.length !== ib.length) return false
  const sa = new Set(ia)
  // 任一侧含重复 id(set 缩水)= 畸形树,不得当作一致放行
  if (sa.size !== ia.length || new Set(ib).size !== ib.length) return false
  return ib.every((id) => sa.has(id))
}

/**
 * 把 tabs 容器内 from 位置的子节点移到 to 位置(**容器内重排**;跨容器移动留后续)。
 * 纯函数、结构共享。active_index **跟随原 active 子节点**的新位置(拖动非当前 tab 不改激活)。
 * 非 tabs 节点 / from 越界 / from===to → 原样返回(no-op)。to clamp 到 [0, len-1]。
 */
export function moveTab(tree: LayoutNode, tabsId: NodeId, from: number, to: number): LayoutNode {
  return updateNode(tree, tabsId, (n) => {
    if (n.kind !== 'tabs') return n
    const len = n.children.length
    if (from < 0 || from >= len) return n
    const dest = Math.min(Math.max(to, 0), len - 1)
    if (from === dest) return n
    const activeChild = n.children[n.active_index]
    const children = [...n.children]
    const [moved] = children.splice(from, 1)
    children.splice(dest, 0, moved)
    const ai = children.indexOf(activeChild)
    return { ...n, children, active_index: ai < 0 ? n.active_index : ai }
  })
}
