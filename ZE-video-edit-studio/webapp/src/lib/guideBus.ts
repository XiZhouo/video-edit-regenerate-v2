/**
 * guideBus.ts — 引导精灵帧的进程内派发点(单 SSE 连接 → GuideLayer)。
 *
 * 为什么存在:引导 SSE 帧(guide_step/guide_end)与实体帧共用 `/api/events`。useDocument 已
 * 持有那条流;它把引导帧转投这里(而非另开一条 EventSource),GuideLayer 订阅即可,零 props 穿透、
 * 单连接。这条总线只搬**呈现帧**(画哪/说什么),从不触碰命令 —— act 的真执行永远经命令总线
 * (dispatchCmd),与本总线无关,故不构成任何旁路。
 */
import type { GuideStepPayload, GuideEndPayload } from './api'

export type GuideFrame = GuideStepPayload | GuideEndPayload
type Listener = (frame: GuideFrame) => void

const listeners = new Set<Listener>()

/** useDocument 主流收到引导帧时调用。 */
export function publishGuideFrame(frame: GuideFrame): void {
  for (const listener of Array.from(listeners)) {
    try {
      listener(frame)
    } catch {
      /* 单个订阅者抛错不影响其余(引导纯视觉,不阻断主流) */
    }
  }
}

/** GuideLayer 订阅;返回退订函数。 */
export function subscribeGuideFrames(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
