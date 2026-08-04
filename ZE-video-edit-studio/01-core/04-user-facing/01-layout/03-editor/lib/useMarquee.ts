/**
 * useMarquee — 橡皮筋框选(框架级,网格/视口通用)
 *
 * 在容器空白处(非 item / 非交互控件)按下左键拖拽 → 命中相交的 item(itemSelector,
 * 元素需带 data-id)→ onSelect(ids, additive)。additive(拖框时按 ⌘/⇧)= 并入已选。
 * 坐标走 client 系 + getBoundingClientRect 相交测试,免 scroll 偏移数学(快速手势,容器内少量滚动可接受)。
 * 返回 ref(挂容器,须 position:relative)+ box(渲染 .ze-marquee 覆盖层)+ 指针 handler。
 */
import { useCallback, useRef, useState } from 'react'

export type MarqueeBox = { left: number; top: number; width: number; height: number }
const INTERACTIVE = 'button,a,input,select,textarea,[role="button"]'

export function useMarquee(
  itemSelector: string,
  onSelect: (ids: string[], additive: boolean) => void,
) {
  const ref = useRef<HTMLDivElement | null>(null)
  const drag = useRef<{ sx: number; sy: number; additive: boolean } | null>(null)
  const [box, setBox] = useState<MarqueeBox | null>(null)

  const hitTest = (l: number, t: number, r: number, b: number): string[] => {
    const el = ref.current
    if (!el) return []
    const ids: string[] = []
    el.querySelectorAll<HTMLElement>(itemSelector).forEach((it) => {
      const q = it.getBoundingClientRect()
      if (q.right >= l && q.left <= r && q.bottom >= t && q.top <= b && it.dataset.id) ids.push(it.dataset.id)
    })
    return ids
  }

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    if (e.button !== 0) return
    const el = ref.current
    if (!el) return
    const target = e.target as HTMLElement
    // 落在卡片 / 交互控件上 → 交给点击/拖拽重排,不起框选
    if (target.closest(itemSelector) || target.closest(INTERACTIVE)) return
    drag.current = { sx: e.clientX, sy: e.clientY, additive: e.metaKey || e.ctrlKey || e.shiftKey }
    try { el.setPointerCapture?.(e.pointerId) } catch { /* 指针捕获非必需,失败忽略 */ }
    setBox({ left: 0, top: 0, width: 0, height: 0 })
  }, [itemSelector])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const d = drag.current
    const el = ref.current
    if (!d || !el) return
    const cr = el.getBoundingClientRect()
    const l = Math.min(d.sx, e.clientX)
    const r = Math.max(d.sx, e.clientX)
    const t = Math.min(d.sy, e.clientY)
    const b = Math.max(d.sy, e.clientY)
    setBox({ left: l - cr.left, top: t - cr.top, width: r - l, height: b - t })
    onSelect(hitTest(l, t, r, b), d.additive)
  }, [onSelect, itemSelector])

  const onPointerUp = useCallback(() => {
    drag.current = null
    setBox(null)
  }, [])

  // pointercancel(触摸中断 / OS 手势接管)也要清理,否则 drag 态与橡皮筋残留
  return { ref, box, onPointerDown, onPointerMove, onPointerUp, onPointerCancel: onPointerUp }
}
