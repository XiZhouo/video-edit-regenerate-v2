/**
 * useEditorShortcuts — 编辑器局部键盘快捷键(框架级)
 *
 * **作用域到聚焦的编辑器**:仅当 rootRef 内含 document.activeElement 才触发,避免与全局
 * 快捷键(⌘S 保存=header、全局 undo=App)双绑。Esc/Del/方向键/局部 ⌘Z 留编辑器自己用。
 * 输入控件(input/textarea/select/contenteditable)聚焦时,文本类键(方向/Del)默认放行给控件。
 */

import { useEffect, type RefObject } from 'react'

export type ShortcutSpec = {
  key: string                 // KeyboardEvent.key,如 'Escape' / 'Delete' / 'z' / 'ArrowLeft'
  meta?: boolean              // ⌘ / Win
  ctrl?: boolean
  shift?: boolean
  /** 控件聚焦时也触发(默认 false:文本控件内放行,避免吞用户输入)*/
  allowInInput?: boolean
  handler: (e: KeyboardEvent) => void
}

function isTextTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  if (!el) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}

export function useEditorShortcuts(
  rootRef: RefObject<HTMLElement | null>,
  specs: ShortcutSpec[],
): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const root = rootRef.current
      if (!root || !root.contains(document.activeElement)) return
      for (const s of specs) {
        if (e.key !== s.key) continue
        if (!!s.meta !== (e.metaKey)) continue
        if (!!s.ctrl !== (e.ctrlKey)) continue
        if (!!s.shift !== (e.shiftKey)) continue
        if (!s.allowInInput && isTextTarget(e.target)) continue
        s.handler(e)
        break
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [rootRef, specs])
}
