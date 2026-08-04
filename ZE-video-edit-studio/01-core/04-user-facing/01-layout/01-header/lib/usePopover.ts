/**
 * usePopover — 菜单弹层 open/close 行为
 *
 * 自实现(v1 形式继承,不依赖外部 Radix / HeadlessUI)
 *
 * 行为:
 *   - click outside trigger / popover → close
 *   - Esc → close + 焦点还给 trigger
 *   - 打开时焦点到 popover 第一个非禁用 menuitem
 */

import {
  useEffect,
  useRef,
  useState,
  type RefObject,
} from 'react'

export type PopoverHandle = {
  open: boolean
  triggerRef: RefObject<HTMLButtonElement | null>
  popoverRef: RefObject<HTMLDivElement | null>
  toggle: () => void
  close: () => void
}

export function usePopover(): PopoverHandle {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)

  // click outside
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node | null
      if (!target) return
      if (popoverRef.current?.contains(target)) return
      if (triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // Esc → close + restore focus
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      triggerRef.current?.focus()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  // 打开时焦点到第一个 menuitem
  useEffect(() => {
    if (!open) return
    const first = popoverRef.current?.querySelector<HTMLElement>(
      '[role="menuitem"]:not([data-disabled="true"])',
    )
    first?.focus()
  }, [open])

  return {
    open,
    triggerRef,
    popoverRef,
    toggle: () => setOpen(prev => !prev),
    close: () => setOpen(false),
  }
}
