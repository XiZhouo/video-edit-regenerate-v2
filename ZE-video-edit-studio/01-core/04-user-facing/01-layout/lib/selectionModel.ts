/**
 * selectionModel — 多选 reducer(框架级,加性叠在既有单选指针之上)
 *
 * 既有 SlotContext.selection(Record<type, id|null>)= "primary/active" 指针(form/asset/canvas 读它,
 * 决定 inspector 显谁),保持不动。本模块加**多选集**:replace(点击)/toggle(ctrl/cmd)/range(shift)。
 * 纯函数、无视图知识 —— 调用方(编辑器)把"当前可见顺序"orderedIds 传进来(表格排序后 / 画廊顺序),
 * range 才能算。table/gallery/task 消费;单选编辑器忽略,零风险。
 */

export type SelectionMode = 'replace' | 'toggle' | 'range' | 'all' | 'none' | 'invert' | 'set' | 'set-add'
export type MultiSelection = Record<string, string[]>

export type SelectMultiFn = (
  entityType: string,
  id: string,
  mode: SelectionMode,
  orderedIds: string[],
) => void

/** 纯 reducer:据 mode 算新选中集 + 新 anchor。orderedIds 仅 range 用。 */
export function applySelect(
  prev: string[],
  id: string,
  mode: SelectionMode,
  orderedIds: string[],
  anchor: string | null,
): { next: string[]; anchor: string | null } {
  if (mode === 'all') {
    return { next: [...orderedIds], anchor: orderedIds[0] ?? null }
  }
  if (mode === 'none') {
    return { next: [], anchor: null }
  }
  if (mode === 'invert') {
    const cur = new Set(prev)
    const next = orderedIds.filter((x) => !cur.has(x))
    return { next, anchor: next[0] ?? null }
  }
  // marquee 框选:orderedIds 此处承载"命中集"。set=替换;set-add=并入(加 ⌘/⇧ 拖框)。
  if (mode === 'set') {
    return { next: [...orderedIds], anchor: orderedIds[0] ?? null }
  }
  if (mode === 'set-add') {
    return { next: [...new Set([...prev, ...orderedIds])], anchor: orderedIds[0] ?? prev[0] ?? null }
  }
  if (mode === 'toggle') {
    const has = prev.includes(id)
    const next = has ? prev.filter((x) => x !== id) : [...prev, id]
    return { next, anchor: id }
  }
  if (mode === 'range' && anchor && orderedIds.length) {
    const a = orderedIds.indexOf(anchor)
    const b = orderedIds.indexOf(id)
    if (a !== -1 && b !== -1) {
      const [lo, hi] = a <= b ? [a, b] : [b, a]
      return { next: orderedIds.slice(lo, hi + 1), anchor }
    }
    // anchor 不在当前顺序里 → 退化为单选
    return { next: [id], anchor: id }
  }
  // replace(默认):再次单击唯一选中项 = 取消选中(F1);多选时先收敛为该项,第二击再取消
  if (prev.length === 1 && prev[0] === id) {
    return { next: [], anchor: null }
  }
  return { next: [id], anchor: id }
}

/** 把鼠标事件修饰键映射成 SelectionMode(ctrl/cmd=toggle、shift=range、否则 replace)。 */
export function selectionModeFromEvent(
  e: { metaKey?: boolean; ctrlKey?: boolean; shiftKey?: boolean },
): SelectionMode {
  if (e.shiftKey) return 'range'
  if (e.metaKey || e.ctrlKey) return 'toggle'
  return 'replace'
}
