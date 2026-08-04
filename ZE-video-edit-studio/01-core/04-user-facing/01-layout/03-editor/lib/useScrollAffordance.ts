/**
 * useScrollAffordance — 把「滚动边缘淡出」从静态装饰升级为状态提示。
 *
 * 背景:`.ze-scroll-fade-y/x/both` 旧版无条件画 mask —— 列表没满 / 滚到底了仍淡出
 * (用户实测「该有的没有、不该有的有」)。纯 CSS 量不到滚动位置,故由本 hook 实测后
 * 命令式直写元素的 `data-scroll-y` / `data-scroll-x` ∈ {none,start,middle,end},
 * CSS 只在对应状态画对应端的 mask(见 utility/utility/code.css 的 [data-scroll-*] 变体)。
 *
 *   none   = 不可滚 → 不画
 *   start  = 在首端(后面还有)→ 只画尾端
 *   end    = 在尾端(前面还有)→ 只画首端
 *   middle = 两端都有 → 两端都画
 *
 * 设计要点:
 *  - **不进 React state**:滚动是高频事件,写 data-* 属性是纯 DOM 副作用,杜绝滚动触发重渲。
 *  - **callback ref**:支持互斥分支里同一 hook 复用(如 OutlinerZone 空/满两 return);元素
 *    挂载即测、卸载即拆。
 *  - **覆盖异步增高**:passive scroll + ResizeObserver(容器+直接子,抓图片加载/段落展开/
 *    生成内容)+ MutationObserver(childList/subtree/characterData,抓流式日志/markdown 重渲)
 *    + window resize 兜底;全部 rAF 合并,unmount 全 disconnect。
 */
import { useCallback, useRef } from 'react'

export type ScrollAxis = 'y' | 'x' | 'both'
type EdgeState = 'none' | 'start' | 'middle' | 'end'

function edgeState(pos: number, size: number, client: number, eps: number): EdgeState {
  if (size - client <= eps) return 'none'       // 内容没溢出 → 不可滚
  if (pos <= eps) return 'start'                // 贴首端
  if (pos + client >= size - eps) return 'end'  // 贴尾端
  return 'middle'
}

/**
 * @param axis  'y'(纵)| 'x'(横)| 'both'(表格类双向)
 * @param epsilon 亚像素/缩放容差,默认 1px
 * @returns callback ref,挂到滚动容器元素上
 */
export function useScrollAffordance(axis: ScrollAxis = 'y', epsilon = 1) {
  const cleanupRef = useRef<(() => void) | null>(null)

  return useCallback((el: HTMLElement | null) => {
    // 元素换走/卸载:拆掉上一组观察器。
    if (cleanupRef.current) {
      cleanupRef.current()
      cleanupRef.current = null
    }
    if (!el) return

    let raf = 0
    const measure = () => {
      raf = 0
      if (axis === 'y' || axis === 'both') {
        el.dataset.scrollY = edgeState(el.scrollTop, el.scrollHeight, el.clientHeight, epsilon)
      }
      if (axis === 'x' || axis === 'both') {
        el.dataset.scrollX = edgeState(el.scrollLeft, el.scrollWidth, el.clientWidth, epsilon)
      }
    }
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(measure)
    }

    const ro = new ResizeObserver(schedule)
    ro.observe(el)
    const observeChildren = () => {
      for (const child of Array.from(el.children)) ro.observe(child)
    }
    observeChildren()
    // 子节点增删(流式日志/列表追加)后既要重新观察新子,也要立即重算。
    const mo = new MutationObserver(() => {
      observeChildren()
      schedule()
    })
    mo.observe(el, { childList: true, subtree: true, characterData: true })

    el.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    measure() // 首帧实测

    cleanupRef.current = () => {
      if (raf) cancelAnimationFrame(raf)
      el.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      ro.disconnect()
      mo.disconnect()
    }
  }, [axis, epsilon])
}
