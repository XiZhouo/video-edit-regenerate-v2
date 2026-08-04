/**
 * studioStore — 画布与时间线共享的**临时视图态**:当前工具、播放头、选中的那一段。
 *
 * 为什么要有它:画布和时间线是两个独立的自定义面板,但它们共用一个播放头
 * (时间线上拖 = 画布跳帧;画布上滚轮擦洗 = 时间线播放头走)。这类东西不是
 * 文档真相 —— 不进 Project Save、不产 Operation,所以不该走 entity。
 *
 * 工具态同理:切工具不改文档、不进 undo、不需要审计,所以它住这儿,不是 Command。
 *
 * 一个极小的发布订阅,配 React 的 useSyncExternalStore 用。
 */

/** 画布上的工具。默认 select —— **按下不该凭空建东西**,那得先挑「画框」。 */
export type ToolId = 'select' | 'rect' | 'text' | 'arrow'

export type Span = { start: number; end: number } | null

/** 选中的那一行,可能是视频图层也可能是标注图层 —— 时间线上它们是同一种东西。 */
export type Picked = { type: 'clip' | 'annotation'; id: string } | null

export type StudioState = {
  /** 当前工具 */
  tool: ToolId
  /** 播放头(秒,时间线坐标) */
  playhead: number
  // 这里**故意没有**「选中的那一段」。发送范围只由标注推导 —— 在时间尺上拖动
  // 只是移动播放头/看画面,它不该、也不能影响发出去的是哪一段。
  // 从前有个 span 当「手动覆盖」,结果随手拖出的 0.17 秒把整条管线带崩了。
  /** 时间线总长(秒),由时间线算好写进来,画布读它画刻度 */
  duration: number
  /** 选中的那一行 */
  picked: Picked
  /** 正在播放 —— 播放时播放头由视频驱动,反向 seek 要让开,否则两头互写会抖 */
  playing: boolean
  /** 正在画框时那一段的实时范围。让时间线在你还没松手的时候就把它高亮出来 ——
   *  滚轮反复来回拨,你得当场看见划出来的是哪一段,而不是松手才知道。 */
  drafting: Span
}

const INITIAL: StudioState = {
  tool: 'select', playhead: 0, duration: 0, picked: null, playing: false,
  drafting: null,
}

let state: StudioState = INITIAL
const listeners = new Set<() => void>()

export function getStudio(): StudioState {
  return state
}

export function subscribeStudio(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/** 逐字段比,全等就不通知(避免 useSyncExternalStore 空转)。
 *  加字段必须同时加进这里 —— 漏一个就是「切了工具界面不动」。 */
function same(a: StudioState, b: StudioState): boolean {
  return a.tool === b.tool && a.playhead === b.playhead
    && a.duration === b.duration && a.picked === b.picked && a.playing === b.playing
    && a.drafting === b.drafting
}

export function setStudio(patch: Partial<StudioState>): void {
  const next = { ...state, ...patch }
  if (same(next, state)) return
  state = next
  listeners.forEach((fn) => fn())
}

/**
 * Creation tools always start a new operation, so an existing selection must
 * stop owning the shared context input. Select mode keeps the current pick.
 */
export function toolActivationPatch(tool: ToolId): Partial<StudioState> {
  return tool === 'select' ? { tool } : { tool, picked: null }
}

/** 把秒数夹进 [0, duration];没有时长信息时只保证非负。 */
export function clampTime(t: number, duration: number): number {
  if (!Number.isFinite(t)) return 0
  const hi = duration > 0 ? duration : Number.MAX_SAFE_INTEGER
  return Math.max(0, Math.min(t, hi))
}

/** 秒 → 「1.50s」;读数要稳定宽度,擦洗时才不跳。 */
export function fmt(t: number): string {
  return `${(Number.isFinite(t) ? t : 0).toFixed(2)}s`
}


// ── 遮挡:与后端 layer_compose.exposed_windows 同一条规则 ──────────────
//
// 一根数轴,两种不透明度:视频片段满屏不透明,盖住它下面的一切;标注只是个框,不盖任何东西。
// 所以某一刻画面 = 最上面那个可见视频层,画在它上面的框 = 层序比它大的可见标注。
// 界面和后端必须同口径 —— 不然按钮亮着、点下去被拒。

type Ent = { id: string; data?: Record<string, any> }
const nz = (v: any): number => (Number.isFinite(Number(v)) ? Number(v) : 0)
const r2 = (v: number) => Math.round(v * 100) / 100

/** 每条标注在 [t0,t1) 里真正露出来的那些小段;完全被盖住的不出现在结果里。 */
export function exposedWindows(clips: Ent[], marks: Ent[],
                               t0: number, t1: number): Map<string, number[][]> {
  const live = clips.filter((c) => c.data?.visible !== false && nz(c.data?.duration) > 0)
  const edges = new Set<number>([t0, t1])
  for (const c of live) {
    for (const e of [nz(c.data?.start), nz(c.data?.start) + nz(c.data?.duration)]) {
      if (e > t0 && e < t1) edges.add(e)
    }
  }
  const ordered = [...edges].sort((a, b) => a - b)
  const topAt = (at: number): Ent | null => {
    let best: Ent | null = null
    for (const c of live) {
      const s = nz(c.data?.start)
      if (at >= s && at < s + nz(c.data?.duration)) {
        if (!best || nz(c.data?.layer) >= nz(best.data?.layer)) best = c
      }
    }
    return best
  }

  const out = new Map<string, number[][]>()
  for (const m of marks) {
    if (m.data?.visible === false) continue
    const a0 = nz(m.data?.start), a1 = a0 + nz(m.data?.duration)
    const mLayer = nz(m.data?.layer)
    const pieces: number[][] = []
    for (let i = 0; i < ordered.length - 1; i += 1) {
      const left = ordered[i], right = ordered[i + 1]
      if (right - left <= 1e-6) continue
      const top = topAt((left + right) / 2)
      if (top && nz(top.data?.layer) > mLayer) continue     // 被上面那层视频盖住
      let lo = Math.max(left, a0), hi = Math.min(right, a1)
      if (a0 === a1 && left <= a0 && a0 <= right) { lo = a0; hi = a0 }
      else if (hi <= lo) continue
      const last = pieces[pieces.length - 1]
      if (last && lo - last[1] < 1e-6) last[1] = Math.max(last[1], hi)
      else pieces.push([r2(lo), r2(hi)])
    }
    if (pieces.length) out.set(m.id, pieces.map(([a, b]) => [r2(a), r2(b)]))
  }
  return out
}
