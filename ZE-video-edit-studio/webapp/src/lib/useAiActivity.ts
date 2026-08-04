/**
 * useAiActivity — AI 行为具象化的前端聚合器(改动回执 / 确认拉起 / 长任务起止)。
 *
 * 消费 aiActivityBus 的事实帧,聚合成精灵可演的「一条当前活动」:
 *   - 回执:AI 改动经 1.2s 归并窗口(与光标停留 1.25s 同节拍)定稿——agentic 一轮连改
 *     只圈最终集合;单实体 → act(飞过去点一下),多实体/批任务 → frame(圈包围盒一次);
 *   - 长任务起止:entity_status 相位转变(→running 起 / running→done|error 止)各回执一次,
 *     常驻进度仍归既有占位/进度条,精灵只做起止提醒;
 *   - 确认拉起:pending_confirmation 帧 → 常驻展示直到批准/忽略;批准走既有网页 nonce
 *     通道(fetchPendingApproval + confirmPendingCommand),**本 hook 不碰命令总线**。
 *
 * 开关:ze.guide.ai_receipt(ops 1.17)管回执与起止;确认拉起是治理面,不受开关影响。
 * tour 进行中回执静默丢弃(教程优先,不抢精灵)。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  confirmPendingCommand, fetchPendingApproval,
  type PendingConfirmationPayload, type RunPhase,
} from './api'
import { publishAiActivity, subscribeAiActivity } from './aiActivityBus'

/** 归并窗口:定稿 §5-3(与光标停留 1.25s 同节拍)。 */
const COALESCE_MS = 1200
/** 回执展示时长(定稿 demo 的可读节拍)。 */
const RECEIPT_HOLD_MS = 2600

export type AiReceiptNote = 'edited' | 'taskStart' | 'taskDone' | 'taskError'

export type AiReceipt = {
  note: AiReceiptNote
  /** 按 entity_type 聚合的目标集(锚点解析在 GuideLayer,本 hook 不碰 DOM)。 */
  entities: { entityType: string; ids: string[] }[]
  count: number
}

export type AiPending = {
  payload: PendingConfirmationPayload
  busy: boolean
  error: string | null
}

export type AiActivity = {
  receipt: AiReceipt | null
  pending: AiPending | null
  approve: () => void
  dismissPending: () => void
}

type Buffer = {
  entities: Map<string, Set<string>>
  note: AiReceiptNote
  count: number
}

const emptyBuffer = (): Buffer => ({ entities: new Map(), note: 'edited', count: 0 })

function bufferAdd(buf: Buffer, entityType: string, id: string): void {
  if (!buf.entities.has(entityType)) buf.entities.set(entityType, new Set())
  if (id) buf.entities.get(entityType)!.add(id)
  buf.count += 1
}

function bufferToReceipt(buf: Buffer): AiReceipt {
  return {
    note: buf.note,
    entities: Array.from(buf.entities.entries()).map(([entityType, ids]) => ({
      entityType, ids: Array.from(ids),
    })),
    count: buf.count,
  }
}

/** 起止判定:非 running → running = 起;running → done/error = 止。 */
export function statusTransition(
  prev: RunPhase | undefined, next: RunPhase,
): 'start' | 'done' | 'error' | null {
  if (next === 'running' && prev !== 'running') return 'start'
  if (prev === 'running' && next === 'done') return 'done'
  if (prev === 'running' && next === 'error') return 'error'
  return null
}

export function useAiActivity(options: {
  projectId?: string
  expectedGeneration?: number
  /** ze.guide.ai_receipt(关则回执/起止静默;确认拉起不受影响)。 */
  receiptEnabled: boolean
  /** tour 进行中 → 回执静默丢弃(教程优先)。 */
  tourActive: boolean
}): AiActivity {
  const { projectId, expectedGeneration, receiptEnabled, tourActive } = options
  const [receipt, setReceipt] = useState<AiReceipt | null>(null)
  const [pending, setPending] = useState<AiPending | null>(null)

  const bufRef = useRef<Buffer>(emptyBuffer())
  const coalesceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const holdTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const phaseRef = useRef<Map<string, RunPhase>>(new Map())
  const optRef = useRef({ receiptEnabled, tourActive })
  optRef.current = { receiptEnabled, tourActive }

  const clearTimers = () => {
    if (coalesceTimerRef.current) { clearTimeout(coalesceTimerRef.current); coalesceTimerRef.current = null }
    if (holdTimerRef.current) { clearTimeout(holdTimerRef.current); holdTimerRef.current = null }
  }

  useEffect(() => {
    const fire = () => {
      coalesceTimerRef.current = null
      const buf = bufRef.current
      bufRef.current = emptyBuffer()
      if (buf.count === 0) return
      if (optRef.current.tourActive || !optRef.current.receiptEnabled) return
      const next = bufferToReceipt(buf)
      setReceipt(next)
      publishAiActivity({ kind: 'receipt_fired', entities: next.entities, holdMs: RECEIPT_HOLD_MS })
      if (holdTimerRef.current) clearTimeout(holdTimerRef.current)
      holdTimerRef.current = setTimeout(() => setReceipt(null), RECEIPT_HOLD_MS)
    }
    const bump = () => {
      if (coalesceTimerRef.current) clearTimeout(coalesceTimerRef.current)
      coalesceTimerRef.current = setTimeout(fire, COALESCE_MS)
    }
    const unsub = subscribeAiActivity((frame) => {
      if (frame.kind === 'ai_change') {
        if (!optRef.current.receiptEnabled || optRef.current.tourActive) return
        for (const change of frame.payload.changes) {
          bufferAdd(bufRef.current, change.entity_type, change.entity_id)
        }
        bump()
        return
      }
      if (frame.kind === 'entity_status') {
        // 快照帧(replace)只对齐相位基线,不触发起止回执(重连不重播历史)。
        const snapshot = frame.payload.replace === true
        for (const s of frame.payload.statuses) {
          const key = `${s.entity_type}:${s.entity_id}`
          const prev = phaseRef.current.get(key)
          phaseRef.current.set(key, s.phase)
          if (snapshot) continue
          if (!optRef.current.receiptEnabled || optRef.current.tourActive) continue
          const transition = statusTransition(prev, s.phase)
          if (!transition) continue
          bufferAdd(bufRef.current, s.entity_type, s.entity_id)
          bufRef.current.note = transition === 'start'
            ? 'taskStart'
            : transition === 'error' ? 'taskError' : 'taskDone'
          bump()
        }
        return
      }
      if (frame.kind === 'pending') {
        // 最新一条优先展示(治理面:不受开关/教程影响)。
        setPending({ payload: frame.payload, busy: false, error: null })
      }
    })
    return () => {
      unsub()
      clearTimers()
    }
  }, [])

  // 切项目 → 清一切瞬时态。
  useEffect(() => {
    clearTimers()
    bufRef.current = emptyBuffer()
    phaseRef.current = new Map()
    setReceipt(null)
    setPending(null)
  }, [projectId])

  const approve = useCallback(() => {
    setPending((cur) => {
      if (!cur || cur.busy || !projectId) return cur
      const token = cur.payload.token
      void (async () => {
        try {
          const material = await fetchPendingApproval(projectId, token)
          if (!material.approval_nonce) throw new Error('approval material unavailable')
          await confirmPendingCommand(
            projectId, token, material.approval_nonce, expectedGeneration ?? 0,
          )
          setPending((now) => (now && now.payload.token === token ? null : now))
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          setPending((now) => (
            now && now.payload.token === token ? { ...now, busy: false, error: message } : now
          ))
        }
      })()
      return { ...cur, busy: true, error: null }
    })
  }, [projectId, expectedGeneration])

  const dismissPending = useCallback(() => setPending(null), [])

  return { receipt, pending, approve, dismissPending }
}

/**
 * useAiHighlightBridge — F2 跨面板高亮桥(App 装配层调用,店主是 useInteractionStore)。
 * 回执定稿 → 把被 AI 改动的实体集写进 highlights,展示期结束清除。
 */
export function useAiHighlightBridge(
  setHighlights: (entityType: string, ids: string[]) => void,
  clearHighlights: (entityType: string) => void,
): void {
  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = []
    const unsub = subscribeAiActivity((frame) => {
      if (frame.kind !== 'receipt_fired') return
      const touched = frame.entities.filter((e) => e.ids.length > 0)
      for (const e of touched) setHighlights(e.entityType, e.ids)
      timers.push(setTimeout(() => {
        for (const e of touched) clearHighlights(e.entityType)
      }, frame.holdMs))
    })
    return () => {
      unsub()
      timers.forEach(clearTimeout)
    }
  }, [setHighlights, clearHighlights])
}

/**
 * useGuideConfig — 读 ze.guide.* 配置(一次性快照;读不到按默认开,不挡路)。
 */
export function useGuideConfig(): { aiReceipt: boolean; sound: boolean } {
  const [cfg, setCfg] = useState({ aiReceipt: true, sound: true })
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const { fetchConfigSnapshot } = await import('./api')
        const snapshot = await fetchConfigSnapshot()
        if (cancelled) return
        setCfg({
          aiReceipt: snapshot['ze.guide.ai_receipt'] !== false,
          sound: snapshot['ze.guide.sound'] !== false,
        })
      } catch {
        /* 配置读不到 → 按默认开 */
      }
    })()
    return () => { cancelled = true }
  }, [])
  return cfg
}
