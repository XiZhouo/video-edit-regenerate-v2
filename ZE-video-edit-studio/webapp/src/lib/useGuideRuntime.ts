/**
 * useGuideRuntime — 引导精灵前端运行时(play / stop / step 推进 + 实时锚点跟踪)。
 *
 * 两个驱动源汇成同一个「当前步」:
 *   - AI 实时:订阅 guideBus 的 guide_step/guide_end(源自 ze_guide,系统 3a 后端);
 *   - 人本地播:playLocal(tour) 逐步走 tour.steps(Help『教我用』)。
 *
 * 红线:运行时**从不执行命令、从不合成 DOM 事件**。act 步在这里只出「幽灵点击」视觉;真执行
 * 要么已由 ze_guide 经命令总线跑完(AI 路),要么由用户自己点被指向的受管按钮(本地路)。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { GuideStep, GuideTour } from './api'
import { subscribeGuideFrames } from './guideBus'
import { effectiveTarget, resolveGuideAnchor, type GuideSelection } from './guideTargets'

export type ActiveGuide = {
  runId: string
  source: 'ai' | 'local'
  index: number
  total: number
  step: GuideStep
}

/** 一步的实时锚点矩形(视口坐标)。null = 无点锚点(精灵居中说)。 */
export type AnchorRect = { top: number; left: number; width: number; height: number } | null

const END_LINGER_MS = 1600

function rectEq(a: AnchorRect, b: AnchorRect): boolean {
  if (a === null || b === null) return a === b
  return (
    Math.round(a.top) === Math.round(b.top)
    && Math.round(a.left) === Math.round(b.left)
    && Math.round(a.width) === Math.round(b.width)
    && Math.round(a.height) === Math.round(b.height)
  )
}

export type GuideRuntime = {
  active: ActiveGuide | null
  rect: AnchorRect
  /** AI run 收尾余晖期(guide_end 已到、精灵尚未淡出);系统 4 用之驱动 dismissing 退场态。 */
  ending: boolean
  playLocal: (tour: GuideTour) => void
  next: () => void
  prev: () => void
  stop: () => void
  canNext: boolean
  canPrev: boolean
  isLocal: boolean
}

export function useGuideRuntime(selection: GuideSelection): GuideRuntime {
  const [active, setActive] = useState<ActiveGuide | null>(null)
  const [rect, setRect] = useState<AnchorRect>(null)
  // 收尾余晖:仅 AI run 的 guide_end 到达后、END_LINGER_MS 淡出前为 true(纯呈现,不触命令)。
  const [ending, setEnding] = useState(false)

  // 本地播的完整步序(AI 路无此,逐帧到达)。
  const localStepsRef = useRef<GuideStep[] | null>(null)
  // 当前 AI run 的 runId(guide_end 只清同一 run)。
  const aiRunRef = useRef<string | null>(null)
  const endTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const selectionRef = useRef<GuideSelection>(selection)
  selectionRef.current = selection

  const clearEndTimer = () => {
    if (endTimerRef.current) {
      clearTimeout(endTimerRef.current)
      endTimerRef.current = null
    }
  }

  const stop = useCallback(() => {
    clearEndTimer()
    localStepsRef.current = null
    aiRunRef.current = null
    setEnding(false)
    setActive(null)
  }, [])

  // ── AI 实时:订阅引导总线 ───────────────────────────────────────────────
  useEffect(() => {
    const unsub = subscribeGuideFrames((frame) => {
      if (frame.type === 'guide_step') {
        clearEndTimer()
        setEnding(false)               // 新步到达:退出上一 run 的收尾余晖
        localStepsRef.current = null   // AI 接管:退出本地播
        aiRunRef.current = frame.runId
        setActive({
          runId: frame.runId,
          source: 'ai',
          index: frame.index,
          total: frame.total,
          step: frame.step,
        })
        return
      }
      // guide_end:仅当收尾的是当前 AI run 才淡出(避免清掉正在进行的本地播)。
      if (frame.runId && frame.runId === aiRunRef.current) {
        clearEndTimer()
        setEnding(true)                // 进入退场态(系统 4:dismissing)
        endTimerRef.current = setTimeout(() => {
          aiRunRef.current = null
          setEnding(false)
          setActive((cur) => (cur && cur.source === 'ai' ? null : cur))
        }, END_LINGER_MS)
      }
    })
    return () => {
      unsub()
      clearEndTimer()
    }
  }, [])

  // ── 人本地播 ─────────────────────────────────────────────────────────
  const playLocal = useCallback((tour: GuideTour) => {
    clearEndTimer()
    aiRunRef.current = null
    setEnding(false)
    const steps = Array.isArray(tour.steps) ? tour.steps : []
    if (steps.length === 0) return
    localStepsRef.current = steps
    setActive({
      runId: `local:${tour.id}`,
      source: 'local',
      index: 0,
      total: steps.length,
      step: steps[0],
    })
  }, [])

  const move = useCallback((delta: number) => {
    setActive((cur) => {
      const steps = localStepsRef.current
      if (!cur || cur.source !== 'local' || !steps) return cur
      const nextIndex = cur.index + delta
      if (nextIndex < 0) return cur
      if (nextIndex >= steps.length) {
        localStepsRef.current = null
        return null   // 走完 → 收起
      }
      return { ...cur, index: nextIndex, step: steps[nextIndex] }
    })
  }, [])

  const next = useCallback(() => move(1), [move])
  const prev = useCallback(() => move(-1), [move])

  // ── 实时锚点跟踪:rAF 轮询 getBoundingClientRect(抗布局/缩放/滚动)──────────
  const step = active?.step ?? null
  // W1-2:作者覆盖 > mod 锚点注册表 > 内核语义 target(见 guideTargets.effectiveTarget)。
  const targetStr = step ? effectiveTarget(step.target, step.presentation) : ''
  useEffect(() => {
    if (!targetStr) {
      setRect(null)
      return
    }
    let raf = 0
    let lastRect: AnchorRect = null
    const tick = () => {
      const el = resolveGuideAnchor(targetStr, selectionRef.current)
      let nextRect: AnchorRect = null
      if (el) {
        const r = el.getBoundingClientRect()
        // 不可见(display:none / 折叠)→ 视作无锚点。
        if (r.width > 0 || r.height > 0) {
          nextRect = { top: r.top, left: r.left, width: r.width, height: r.height }
        }
      }
      if (!rectEq(nextRect, lastRect)) {
        lastRect = nextRect
        setRect(nextRect)
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [targetStr])

  const isLocal = active?.source === 'local'
  const localLen = localStepsRef.current?.length ?? 0
  return {
    active,
    rect,
    ending,
    playLocal,
    next,
    prev,
    stop,
    canNext: isLocal && !!active && active.index < localLen - 1,
    canPrev: isLocal && !!active && active.index > 0,
    isLocal,
  }
}
