/**
 * ContextMenu — 声明式右键菜单(框架级 primitive,与 header 菜单解耦)
 *
 * 复用皮肤原子 .ze-popover / .ze-menu-item / .ze-menu-group(不 import header 组件,守面板解耦)。
 * portal 到 body,锚定 useContextMenu 的 {x,y},measure 后 clamp 进视口。items 声明式:
 * copy/download 由编辑器在 onSelect 里走本地 DOM,delete/rename/duplicate/adopt 走 dispatch。
 */

import { useLayoutEffect, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import type { ContextMenuState } from './useContextMenu'

export type ContextMenuItem = {
  id: string
  label: ReactNode
  shortcut?: string
  variant?: 'default' | 'danger'
  disabled?: boolean
  onSelect: () => void
}

export type ContextMenuProps = {
  state: ContextMenuState
  items: (ContextMenuItem | 'separator')[]
  onClose: () => void
  menuRef: RefObject<HTMLDivElement | null>
}

/** 把 items 按 'separator' 切成组,组间皮肤自动出分割线。 */
function groupItems(items: (ContextMenuItem | 'separator')[]): ContextMenuItem[][] {
  const groups: ContextMenuItem[][] = [[]]
  for (const it of items) {
    if (it === 'separator') groups.push([])
    else groups[groups.length - 1].push(it)
  }
  return groups.filter((g) => g.length > 0)
}

export function ContextMenu({ state, items, onClose, menuRef }: ContextMenuProps): ReactNode {
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: state.x, top: state.y })

  // 打开 / 坐标变 → 先放到点击点,measure 后 clamp 进视口
  useLayoutEffect(() => {
    if (!state.open) return
    const el = menuRef.current
    let left = state.x
    let top = state.y
    if (el) {
      const r = el.getBoundingClientRect()
      if (left + r.width > window.innerWidth) left = Math.max(4, window.innerWidth - r.width - 4)
      if (top + r.height > window.innerHeight) top = Math.max(4, window.innerHeight - r.height - 4)
    }
    setPos({ left, top })
  }, [state.open, state.x, state.y, menuRef])

  if (!state.open) return null

  return createPortal(
    <div
      ref={menuRef}
      className="ze-popover"
      data-context="true"
      data-open="true"
      role="menu"
      style={{ left: pos.left, top: pos.top }}
    >
      {groupItems(items).map((group, gi) => (
        <div key={gi} className="ze-menu-group" role="group">
          {group.map((it) => (
            <button
              key={it.id}
              type="button"
              className="ze-menu-item"
              role="menuitem"
              data-variant={it.variant ?? 'default'}
              data-disabled={it.disabled ? 'true' : undefined}
              disabled={it.disabled}
              onClick={it.disabled ? undefined : () => { it.onSelect(); onClose() }}
            >
              <span>{it.label}</span>
              {it.shortcut && <kbd className="ze-menu-item__shortcut">{it.shortcut}</kbd>}
            </button>
          ))}
        </div>
      ))}
    </div>,
    document.body,
  )
}
