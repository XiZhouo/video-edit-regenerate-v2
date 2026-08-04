/**
 * AssistantRoot — 单一 AI 助手的**唯一根**(W1-1,2026-07-26 用户令)。
 *
 * 在此之前:AppChrome 里 `<CopilotDock>` 与 `<GuideLayer>` 是两个并列兄弟,各自独立
 * 挂载、各自定位。用户的判词是「聊天框是它的分身,不是精灵自己展开形成的」——
 * 这不是观感问题,是结构事实:两棵互不相干的树,面板不可能从精灵身上长出来。
 *
 * 现在:本组件是那个唯一的根。精灵(由宿主经 `sprite` 传入,因为它住在 webapp/src、
 * core 不能反向 import)与对话面板都是它的孩子。
 *
 * 【展开起点怎么定】精灵经 assistantBus 播 `sprite_pos`(视口坐标)。面板要的是
 * **相对自己盒子**的 transform-origin,换算必须在这里做 —— 不能把面板尺寸
 * (min(380px,…) / min(560px,…))抄进 CSS 再 calc,那是把同一个事实写两处,
 * 面板改宽就悄悄错位。开合翻转的那一刻量一次面板矩形,以**翻转前**精灵所在的位置
 * 作起点:那正是「从它待着的地方撑开」。
 *
 * 设计真源:《ZE AI 助手 · 精灵即面板》2026-07-21 §3.2 五阶段动线契约
 *   ① 触发(点精灵)→ ② 滑向停靠角 → ③ 变形撑开(圆 → 圆角矩形,morph 380ms +
 *   撑开 520ms)→ ④ 头像凝结(面板头部 18px 呼吸点 = 精灵在家的样子)→ ⑤ 逆放收起。
 * 本组件负责 ①③⑤ 的**结构与起点**;②④ 分别由 spriteEngine 的 panel 停靠与
 * CopilotDock 头部的 `.ze-copilot__pulse` 承担。
 *
 * 分层:只发 className + data-* + 两个坐标 CSS 变量(定位事实,非视觉决策);
 * 全部视觉在 03-css-rules 的 copilot-shell。
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { CopilotDock, type CopilotDockProps } from './CopilotDock'
import { publishAssistant, subscribeAssistant } from './assistantBus'

export type AssistantRootProps = CopilotDockProps & {
  /** 精灵层(GuideLayer)。由宿主传入:它住在 webapp/src,core 不能 import 它。 */
  sprite?: ReactNode
}

export function AssistantRoot({ sprite, ...dockProps }: AssistantRootProps) {
  const [open, setOpen] = useState(false)
  const [side, setSide] = useState<'left' | 'right'>('right')
  const rootRef = useRef<HTMLDivElement>(null)
  /** 精灵最近一次停泊中心(视口坐标)。用 ref:它每次移动都变,走 state 会重渲整棵助手树。 */
  const spritePosRef = useRef<{ x: number; y: number } | null>(null)

  useEffect(() => subscribeAssistant((frame) => {
    if (frame.type === 'chat_state') { setOpen(frame.open); setSide(frame.side) }
    else if (frame.type === 'sprite_pos') { spritePosRef.current = { x: frame.x, y: frame.y } }
  }), [])

  // 面板矩形 → 精灵(它的 panel 停靠形态要拿它算落点)。开合与视口变化时各量一次。
  useEffect(() => {
    const measure = () => {
      const panel = rootRef.current?.querySelector<HTMLElement>('.ze-copilot__panel')
      const box = panel?.getBoundingClientRect()
      publishAssistant({
        type: 'panel_rect',
        rect: box && box.width > 0 && box.height > 0
          ? { top: box.top, left: box.left, width: box.width, height: box.height }
          : null,
      })
    }
    measure()
    if (!open) return
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [open])

  // 开合翻转 → 把展开起点钉在精灵身上。面板此刻刚渲染出来,量它的盒子做换算。
  useEffect(() => {
    const root = rootRef.current
    const pos = spritePosRef.current
    if (!root || !pos) return
    const panel = root.querySelector<HTMLElement>('.ze-copilot__panel')
    if (!panel) return
    const box = panel.getBoundingClientRect()
    if (box.width <= 0 || box.height <= 0) return
    // 夹进面板盒内:精灵可能停在面板外侧(panel 形态停靠在左上角外),起点跑到盒外
    // 会让缩放看着像从屏幕别处飞来,而不是「从它那儿长出来」。
    const ox = Math.min(Math.max(pos.x - box.left, 0), box.width)
    const oy = Math.min(Math.max(pos.y - box.top, 0), box.height)
    root.style.setProperty('--ze-assistant-origin-x', `${Math.round(ox)}px`)
    root.style.setProperty('--ze-assistant-origin-y', `${Math.round(oy)}px`)
  }, [open])

  return (
    <div
      ref={rootRef}
      className="ze-assistant"
      data-ze-assistant-root=""
      /* W1-2:`guide:assistant` 的锚点。教学里「跟 AI 说你要什么」这类步骤指的就是助手本身,
         它不是命令按钮也不是实体,只能靠呈现锚点。`guide:` 这一类此前全舰队零产出。 */
      data-ze-guide-target="assistant"
      data-open={open ? 'true' : 'false'}
      data-side={side}
    >
      {sprite}
      <CopilotDock {...dockProps} />
    </div>
  )
}
