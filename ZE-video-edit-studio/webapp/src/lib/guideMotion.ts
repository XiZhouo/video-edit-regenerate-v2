/**
 * guideMotion.ts — 引导精灵运动引擎的纯逻辑(W2e 系统 4『guide-shell』)。
 *
 * 把「当前步语义 + 有无锚点 rect + 收尾态」映射成:
 *   - 8 态状态机 GuideState:hidden/appearing/idle/moving/pointing/framing/acting/dismissing;
 *   - 形态 GuideForm:orb / cursor / frame(由状态派生,transient 期恒为 orb → 精灵先飞到位再 morph)。
 *
 * 状态机是纯呈现层 FSM(不触命令、不产生 Operation):
 *   settled(稳态)由 deriveSettledState 从 step.kind + hasRect 求;
 *   transient(过渡态,appearing/moving)由 useGuideMotion 用短时窗计时叠加;
 *   dismissing 由 runtime 的 ending(guide_end 余晖)驱动;hidden 由 active 缺席驱动。
 *
 * 形态从步骤语义自动选(设计 §4.1):say/wait/ask→orb;point→cursor;highlight→frame;act→cursor。
 * 作者可用 step.presentation.form 覆盖(设计『作者可用步骤 form 覆盖』)。
 */
import { useEffect, useRef, useState } from 'react'
import type { GuideStepKind } from './api'

export type GuideForm = 'orb' | 'cursor' | 'frame'

/** 8 态封闭集(生命周期 FSM;设计 §4.6 的可视态收敛为此 8 节点,speaking/listening/thinking 归 idle-orb 变体)。 */
export type GuideState =
  | 'hidden'
  | 'appearing'
  | 'idle'
  | 'moving'
  | 'pointing'
  | 'framing'
  | 'acting'
  | 'dismissing'

export type TransientState = 'appearing' | 'moving'

/** 过渡态时窗(ms):首现放大回弹 / 换靶飞行(与 CSS moveTo --ze-spring 时长大致同拍)。 */
export const APPEAR_MS = 340
export const MOVE_MS = 420

/** step.presentation.form 覆盖(逻辑层恒为 {},此为前端作者覆盖位)。 */
export function formOverride(presentation: Record<string, unknown> | undefined): GuideForm | null {
  const v = presentation?.form
  return v === 'orb' || v === 'cursor' || v === 'frame' ? v : null
}

/**
 * 稳态形态:step.kind + 有无锚点 rect → 形态(缺锚点一律回落 orb「只说不指」)。
 * 优先级:作者 form 覆盖 > 语义默认。
 */
export function deriveForm(
  kind: GuideStepKind | undefined,
  hasRect: boolean,
  override?: GuideForm | null,
): GuideForm {
  if (override) return override
  if (!kind) return 'orb'
  if (kind === 'highlight') return hasRect ? 'frame' : 'orb'
  if (kind === 'point' || kind === 'act') return hasRect ? 'cursor' : 'orb'
  return 'orb' // say / wait / ask
}

/** 稳态状态(不含 hidden/appearing/moving/dismissing 这些由生命周期时窗决定的态)。 */
export function deriveSettledState(
  kind: GuideStepKind | undefined,
  hasRect: boolean,
  override?: GuideForm | null,
): GuideState {
  const form = deriveForm(kind, hasRect, override)
  if (kind === 'act') return 'acting'
  if (form === 'cursor') return 'pointing'
  if (form === 'frame') return 'framing'
  return 'idle'
}

/**
 * 合成最终状态(生命周期时窗优先级从高到低):
 *   无 active → hidden;收尾余晖 → dismissing;过渡时窗 → appearing/moving;否则 → 稳态。
 */
export function resolveState(
  hasActive: boolean,
  ending: boolean,
  transient: TransientState | null,
  settled: GuideState,
): GuideState {
  if (!hasActive) return 'hidden'
  if (ending) return 'dismissing'
  if (transient) return transient
  return settled
}

/** 状态 → 形态:过渡/生命周期态恒为 orb(精灵先以球飞到位/退场),定位态才 morph 成 cursor/frame。 */
export function stateForm(
  state: GuideState,
  kind: GuideStepKind | undefined,
  hasRect: boolean,
  override?: GuideForm | null,
): GuideForm {
  if (state === 'pointing' || state === 'acting') return deriveForm(kind, hasRect, override) === 'orb' ? 'orb' : 'cursor'
  if (state === 'framing') return 'frame'
  return 'orb'
}

/**
 * useGuideMotion — 过渡态时窗跟踪(纯计时,无 DOM 轮询;复用 activeKey 变化作为唯一触发)。
 *   activeKey null→值 = 首现(appearing);值→另一值 = 换靶(moving);到时清空回稳态。
 *   ending 期间不再触发过渡(退场优先)。
 */
export function useGuideMotion(activeKey: string | null, ending: boolean): TransientState | null {
  const [transient, setTransient] = useState<TransientState | null>(null)
  const prevKeyRef = useRef<string | null>(null)

  useEffect(() => {
    const prev = prevKeyRef.current
    prevKeyRef.current = activeKey
    if (!activeKey || ending) {
      setTransient(null)
      return
    }
    const kind: TransientState = prev === null ? 'appearing' : 'moving'
    setTransient(kind)
    const ms = kind === 'appearing' ? APPEAR_MS : MOVE_MS
    const timer = setTimeout(() => setTransient(null), ms)
    return () => clearTimeout(timer)
  }, [activeKey, ending])

  return ending ? null : transient
}
