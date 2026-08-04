/**
 * useContextMenu — 右键菜单 open/close + 指针锚定(框架级 primitive)
 *
 * 行为继承 header 的 usePopover 范式(click-outside / Esc 关),但锚定在鼠标点(clientX/Y)。
 * 绑到任意 行/卡/单元 的 onContextMenu;preventDefault + 记坐标。滚动 / resize 也关。
 */

import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

export type ContextMenuState = { open: boolean; x: number; y: number }

export type UseContextMenu = {
  state: ContextMenuState
  onContextMenu: (e: React.MouseEvent) => void
  close: () => void
  menuRef: RefObject<HTMLDivElement | null>
}

export function useContextMenu(): UseContextMenu {
  const [state, setState] = useState<ContextMenuState>({ open: false, x: 0, y: 0 })
  const menuRef = useRef<HTMLDivElement>(null)

  const close = useCallback(() => setState((s) => (s.open ? { ...s, open: false } : s)), [])

  const onContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setState({ open: true, x: e.clientX, y: e.clientY })
  }, [])

  useEffect(() => {
    if (!state.open) return
    const onDown = (e: MouseEvent) => {
      if (menuRef.current?.contains(e.target as Node)) return
      close()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    const onScroll = () => close()
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onScroll)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onScroll)
    }
  }, [state.open, close])

  return { state, onContextMenu, close, menuRef }
}
