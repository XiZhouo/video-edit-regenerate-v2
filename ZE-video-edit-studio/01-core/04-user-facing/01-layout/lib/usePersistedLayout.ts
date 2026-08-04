/**
 * usePersistedLayout — LayoutTree 的 localStorage 持久化(SPEC §2.5)
 *
 * key = `ze.layout.<skill_id>.<doc_id>`。
 *
 * 时序:docId 来自后端(meta.id),首渲染时还没到。所以:
 *   - docId 缺席:tree = fallback(纯内存,不读不写)
 *   - docId 到达:读一次 localStorage(命中则用,否则 fallback)
 *   - update():setState + 立即写 localStorage(在事件回调里写,不走 effect,避免
 *     "key 变化的同帧 effect 用旧 tree 覆盖新 key"的竞态)
 *
 * 读失败 / 损坏 / 私密模式配额 → 静默回退,绝不抛。
 *
 * 注意:`fallback` 必须是稳定引用(模块级常量)。它故意不入读 effect 的 deps —
 * 若传内联 / 每次 render 新建的对象,key 确定后的 fallback 变化会被忽略。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { sameNodeIdSet } from './layoutOps'
import type { LayoutNode } from '../types'

/**
 * 用新鲜 default(modLayout)的节点声明配置覆盖持久化树的同 id 节点。
 *
 * **持久化树只保「结构」(split ratio / tabs active_index / tab 顺序 / 面板拼法),不保 mod 声明配置**
 * (slot 的 editor_class / entity_bindings / name/nameKey / view_state / zone_overrides,
 *  以及 split/tabs 的 name/nameKey)。否则 mod 改了布局声明
 * (编辑器改名、管线显示开关、换绑定…)会被用户浏览器里的旧持久化布局**架空**——部署了也不生效
 * (用户实测:横栏名/管线显示改了线上没变,根因即此)。结构归用户、声明归 mod,各取一半。
 */
export function overlayModDeclarations(tree: LayoutNode, mod: LayoutNode): LayoutNode {
  const modNodes = new Map<string, LayoutNode>()
  const collect = (n: LayoutNode): void => {
    modNodes.set(n.id, n)
    if (n.kind === 'split') { collect(n.first); collect(n.second) }
    else if (n.kind === 'tabs') n.children.forEach(collect)
  }
  collect(mod)
  const apply = (n: LayoutNode): LayoutNode => {
    const m = modNodes.get(n.id)
    if (n.kind === 'slot') {
      return m?.kind === 'slot'
        ? { ...n, editor_class: m.editor_class, entity_bindings: m.entity_bindings, name: m.name, nameKey: m.nameKey, view_state: m.view_state, zone_overrides: m.zone_overrides }
        : n
    }
    if (n.kind === 'split') {
      return {
        ...n,
        name: m?.kind === 'split' ? m.name : n.name,
        nameKey: m?.kind === 'split' ? m.nameKey : n.nameKey,
        first: apply(n.first),
        second: apply(n.second),
      }
    }
    return {
      ...n,
      name: m?.kind === 'tabs' ? m.name : n.name,
      nameKey: m?.kind === 'tabs' ? m.nameKey : n.nameKey,
      children: n.children.map(apply),
    }
  }
  return apply(tree)
}

export const overlayModSlots = overlayModDeclarations

/**
 * 读持久化树并对当前 default 校验:节点 id 集不一致 → 弃用(SPEC §2.5)。
 * 用户能改的只有 ratio / active_index;增删节点的运行时入口已移除,id 集不符
 * 说明是污染树(旧分屏功能写入的 n-* 空槽)或 mod 换过 default 布局 —— 滞留无法清除。
 * id 集一致时:取持久化的结构、但 mod 声明配置用新鲜 default 覆盖(overlayModDeclarations)。
 */
function readLayout(key: string, fallback: LayoutNode): LayoutNode | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { kind?: string }
    if (parsed && (parsed.kind === 'slot' || parsed.kind === 'split' || parsed.kind === 'tabs')) {
      const tree = parsed as LayoutNode
      return sameNodeIdSet(tree, fallback) ? overlayModDeclarations(tree, fallback) : null
    }
    return null
  } catch {
    return null
  }
}

function writeLayout(key: string, tree: LayoutNode): void {
  try {
    localStorage.setItem(key, JSON.stringify(tree))
  } catch {
    /* 配额 / 私密模式:静默 */
  }
}

export function usePersistedLayout(
  skillId: string,
  docId: string | undefined,
  fallback: LayoutNode,
): [LayoutNode, (tree: LayoutNode) => void] {
  const [tree, setTree] = useState<LayoutNode>(fallback)
  const keyRef = useRef<string | null>(null)

  // docId 到达(或变化)时读一次
  useEffect(() => {
    if (!docId) return
    const key = `ze.layout.${skillId}.${docId}`
    if (keyRef.current === key) return
    keyRef.current = key
    setTree(readLayout(key, fallback) ?? fallback)
    // fallback 是稳定常量(装配层模块级),故意不入 deps
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skillId, docId])

  const update = useCallback((next: LayoutNode) => {
    setTree(next)
    if (keyRef.current) writeLayout(keyRef.current, next)
  }, [])

  return [tree, update]
}
