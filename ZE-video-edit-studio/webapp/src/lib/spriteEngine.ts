/**
 * spriteEngine.ts — 精灵变形体渲染引擎(系统 4 视觉层 v2,替换 4 层 Siri 美术)。
 *
 * 设计真源:《ZE精灵变形体视觉设计-20260718》sprite-v2.html(用户 10 轮迭代定稿);
 * 本文件是其工程移植:**参数照搬定稿默认值(PARAMS),不原创**。权威契约见 MOTION.md。
 *
 * 本体 = 一个变形虫式实体:白芯蓝边、accent 主题色发光。三形态严格连续变形:
 *   球(28 点圆) ⇄ 三角光标(28 点箭头) ⇄ 环形矩形(外环 16 + 内环 12,evenodd 镂空环带)。
 * 光只有一层:移动余晖(canvas 每帧重建的锥形雾带,按时长精确消散零残留)。
 *
 * 分层边界(不越界):
 *   - 引擎只消费 GuideLayer 派生的 SpriteMode + 锚点 rect,**不碰 FSM/运行时/命令总线**;
 *   - 纯视觉:从不合成 DOM 事件、从不执行命令(治理红线与 GuideLayer 同款);
 *   - 颜色全部来自 --ze-* 令牌(canvas 侧经探针解析成具体色,随主题/accent 自动跟随);
 *   - reduced-motion(prefers-reduced-motion / html[data-motion="off"]):瞬移+关余晖/拉伸/
 *     变形动画/涟漪/音效,位置、圈选压暗、气泡、信息全保留。
 */

/** §7.3 P0-C 新增 `panel`:精灵停靠在助手面板旁 —— **保留存在感,但不抢戏**。
 *  与 point/act 的区别:那两个是「精灵在演示某处」(morph 到指点形态、有到达特效);panel 是
 *  「主要信息在可读面板里,精灵只是它的锚」,故停靠后回圆球静息态、不做指点动作。 */
export type SpriteMode = 'hidden' | 'rest' | 'point' | 'act' | 'frame' | 'panel' | 'dismiss'

export type SpriteRectPx = { top: number; left: number; width: number; height: number }

export type SpriteInput = {
  mode: SpriteMode
  /** point/act/frame 的目标锚点(视口坐标);其余模式忽略。 */
  rect: SpriteRectPx | null
  /** 待机/说话的停靠中心点(视口坐标)。 */
  rest: { x: number; y: number }
  /** 点击 tick 音效开关(默认开;AudioContext 不可用时静默)。 */
  sound: boolean
  /** thinking 变体:助手忙碌(对话进行中)→ 呼吸加速 2.4s→1.4s(定稿 §5-1,气泡三点仍是主信号)。 */
  thinking?: boolean
}

/* ═══════════ 定稿参数(sprite-v2 §5 全参数表;照搬,唯一真源) ═══════════ */
export const SPRITE_PARAMS = {
  // 光标形状(三角三要素 + 尾缘折深 + 整体缩放)
  tipY: 1.6, wingHalf: 7.6, wingY: 15.8, fold: 2.4, aScale: 1,
  // 球体
  orbR: 8.2, breatheAmp: 0.05, breatheDur: 2400,
  // 矩形框
  framePad: 10, frameRadius: 12, frameBorder: 5, frameGlow: 0.45, fillA: 0.06,
  veilDark: 0.6, veilLight: 0.10,
  // §7.3 面板停靠(panel mode):离面板左边缘的水平间距 / 距面板顶的垂直下沉,兼作视口夹取下限
  panelDock: 22, panelDockTop: 18,
  // 运动
  speed: 1, bow: 1, wobble: 1, squashMax: 1.4, turnLead: 1,
  // 变形
  morphDur: 380, springS: 1.55,
  // 余晖
  trailAge: 700, trailW: 1, trailA: 1,
  // 节拍
  clickHold: 1250, exitDur: 380, appearDur: 420, frameInDur: 520, frameOutDur: 420,
} as const

const P = SPRITE_PARAMS
const N = 28
const VB = 24            // 常态 viewBox 边长
const SPRITE_PX = 34     // 常态渲染尺寸(px)
const HALF = SPRITE_PX / 2
const IDLE_ROT = -38

/* ═══════════ 形状库(球/箭头同 28 顶点;环 = 外 16 + 内 12) ═══════════ */
type Pt = [number, number]

function circlePts(): Pt[] {
  const pts: Pt[] = []
  for (let i = 0; i < N; i++) {
    const a = -Math.PI / 2 + (i / N) * Math.PI * 2
    pts.push([VB / 2 + P.orbR * Math.cos(a), VB / 2 + P.orbR * Math.sin(a)])
  }
  return pts
}

function subdiv(verts: Pt[]): Pt[] {
  const edges: { x1: number; y1: number; x2: number; y2: number; len: number }[] = []
  let total = 0
  for (let i = 0; i < verts.length - 1; i++) {
    const [x1, y1] = verts[i]
    const [x2, y2] = verts[i + 1]
    const len = Math.hypot(x2 - x1, y2 - y1)
    edges.push({ x1, y1, x2, y2, len })
    total += len
  }
  const per = edges.map((e) => Math.max(1, Math.round((e.len / total) * N)))
  while (per.reduce((a, b) => a + b, 0) > N) per[per.indexOf(Math.max(...per))]--
  while (per.reduce((a, b) => a + b, 0) < N) per[per.indexOf(Math.min(...per))]++
  const pts: Pt[] = []
  edges.forEach((e, i) => {
    for (let j = 0; j < per[i]; j++) {
      const t = j / per[i]
      pts.push([e.x1 + (e.x2 - e.x1) * t, e.y1 + (e.y2 - e.y1) * t])
    }
  })
  return pts
}

function arrowPts(): Pt[] {
  const cx = VB / 2
  const cy = VB / 2
  const raw: Pt[] = [
    [cx, P.tipY],
    [cx + P.wingHalf, P.wingY],
    [cx, P.wingY - P.fold],
    [cx - P.wingHalf, P.wingY],
    [cx, P.tipY],
  ]
  const scaled = raw.map(([x, y]): Pt => [cx + (x - cx) * P.aScale, cy + (y - cy) * P.aScale])
  const pts = subdiv(scaled)
  let minI = 0
  pts.forEach((pt, i) => { if (pt[1] < pts[minI][1]) minI = i })
  pts.push(...pts.splice(0, minI))
  return pts
}

const CIRCLE = circlePts()
const ARROW = arrowPts()

const pathD = (pts: Pt[]): string =>
  'M' + pts.map((p) => p[0].toFixed(2) + ',' + p[1].toFixed(2)).join('L') + 'Z'

/* 圆角矩形解析路径(稳态,角部完美) */
function rrD(x0: number, y0: number, x1: number, y1: number, r: number): string {
  return `M ${x0 + r} ${y0} H ${x1 - r} A ${r} ${r} 0 0 1 ${x1} ${y0 + r} V ${y1 - r} A ${r} ${r} 0 0 1 ${x1 - r} ${y1} H ${x0 + r} A ${r} ${r} 0 0 1 ${x0} ${y1 - r} V ${y0 + r} A ${r} ${r} 0 0 1 ${x0 + r} ${y0} Z`
}

/* 圆角矩形采样(变形过渡):8 段固定点数,直边 lc 点/弧 ac 点 */
function rrPts(x0: number, y0: number, x1: number, y1: number, r: number, lc: number, ac: number): Pt[] {
  const pts: Pt[] = []
  const line = (ax: number, ay: number, bx: number, by: number, n: number) => {
    for (let j = 0; j < n; j++) { const t = j / n; pts.push([ax + (bx - ax) * t, ay + (by - ay) * t]) }
  }
  const arc = (cx: number, cy: number, a0: number, n: number) => {
    for (let j = 0; j < n; j++) { const a = a0 + (j / n) * (Math.PI / 2); pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]) }
  }
  line(x0 + r, y0, x1 - r, y0, lc); arc(x1 - r, y0 + r, -Math.PI / 2, ac)
  line(x1, y0 + r, x1, y1 - r, lc); arc(x1 - r, y1 - r, 0, ac)
  line(x1 - r, y1, x0 + r, y1, lc); arc(x0 + r, y1 - r, Math.PI / 2, ac)
  line(x0, y1 - r, x0, y0 + r, lc); arc(x0 + r, y0 + r, Math.PI, ac)
  return pts
}

type Ring = { outer: Pt[]; inner: Pt[]; outerD: string; innerD: string }
function ringPts(w: number, h: number): Ring {
  const band = Math.max(1, Math.min(P.frameBorder, w / 2 - 2, h / 2 - 2))
  const rOut = Math.max(2, Math.min(P.frameRadius, w / 2, h / 2))
  const rIn = Math.max(1, rOut - band)
  return {
    outer: rrPts(1.2, 1.2, w - 1.2, h - 1.2, rOut, 2, 2),
    inner: rrPts(1.2 + band, 1.2 + band, w - 1.2 - band, h - 1.2 - band, rIn, 1, 2),
    outerD: rrD(1.2, 1.2, w - 1.2, h - 1.2, rOut),
    innerD: rrD(1.2 + band, 1.2 + band, w - 1.2 - band, h - 1.2 - band, rIn),
  }
}

/* ═══════════ 缓动(照搬) ═══════════ */
const easeInOutCubic = (t: number): number => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)
const easeOutBack = (t: number): number => {
  const s = P.springS
  return 1 + (s + 1) * Math.pow(t - 1, 3) + s * Math.pow(t - 1, 2)
}

/* ═══════════ 停泊点(精灵与气泡共用的确定性函数;GuideLayer 也 import) ═══════════ */
export function parkPoint(
  mode: SpriteMode,
  rect: SpriteRectPx | null,
  rest: { x: number; y: number },
): { x: number; y: number } {
  if ((mode === 'point' || mode === 'act') && rect) {
    return { x: rect.left + rect.width / 2, y: Math.max(60, rect.top - 26) }
  }
  if (mode === 'frame' && rect) {
    // 气泡锚点:环带右上角(sprite-v2 placeBubble 的 framed 分支)
    return { x: rect.left + rect.width + P.framePad - 14, y: rect.top - P.framePad + 8 }
  }
  if (mode === 'panel' && rect) {
    // §7.3 面板态:停在面板**左上角外侧**——不压住面板内容(可读性优先),又贴着它表明归属;
    // 视口夹取防止面板贴边时精灵被挤出屏外。
    return { x: Math.max(P.panelDock, rect.left - P.panelDock),
             y: Math.max(P.panelDockTop, rect.top + P.panelDockTop) }
  }
  return rest
}

/** reduced-motion:系统偏好或皮肤根级 data-motion="off"(MOTION.md 全局降级合同)。 */
export function motionOff(): boolean {
  if (typeof window === 'undefined') return true
  if (document.documentElement.dataset.motion === 'off') return true
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/* ═══════════ 引擎 ═══════════ */
type Pose = { x: number; y: number; w: number; h: number }
type Flight = {
  x0: number; y0: number; cx: number; cy: number; x1: number; y1: number
  t0: number; dur: number; wob: number
}
type FrameAnim = { dir: 'in' | 'out'; t0: number; dur: number; fromPts: Pt[]; fromBox: Pose; toBox: Pose }
type TrailPt = { x: number; y: number; t: number }

export class SpriteEngine {
  private host: HTMLElement
  private cv: HTMLCanvasElement
  private ctx: CanvasRenderingContext2D | null
  private glow: HTMLDivElement
  private svg: SVGSVGElement
  private veilPath: SVGPathElement
  private washPath: SVGPathElement
  private shape: SVGPathElement

  private input: SpriteInput = { mode: 'hidden', rect: null, rest: { x: 60, y: 300 }, sound: true }
  private destKey = ''
  private visible = false
  private raf = 0
  private lastT = 0
  private frameCount = 0

  // 位姿/形态(与 sprite-v2 同名同义)
  private pose: Pose = { x: 0, y: 0, w: SPRITE_PX, h: SPRITE_PX }
  private form: 0 | 1 = 0
  private morphFrom: Pt[] = CIRCLE
  private morphTo: Pt[] = CIRCLE
  private morphT0 = 0
  private morphing = false
  private flying = false
  private flight: Flight | null = null
  private arriveCb: (() => void) | null = null
  private rot = IDLE_ROT
  private rotT = IDLE_ROT
  private squash = 1
  private velNow = { x: 0, y: 0 }
  private framed = false
  private frameAnim: FrameAnim | null = null
  private trail: TrailPt[] = []
  private pendingTimer: ReturnType<typeof setTimeout> | null = null
  private ac: AudioContext | null = null

  // canvas 侧解析色(探针;每 ~60 帧刷新,随主题/accent 跟随)
  private accentColor = 'rgb(90, 130, 235)'
  private hiColor = 'rgb(170, 195, 250)'
  private probe: HTMLSpanElement

  constructor(host: HTMLElement) {
    this.host = host
    this.cv = document.createElement('canvas')
    this.cv.className = 'ze-guide__trail'
    this.ctx = this.cv.getContext('2d')
    this.glow = document.createElement('div')
    this.glow.className = 'ze-guide__glow'
    this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    this.svg.setAttribute('class', 'ze-guide__svg')
    this.svg.setAttribute('viewBox', `0 0 ${VB} ${VB}`)
    this.veilPath = document.createElementNS('http://www.w3.org/2000/svg', 'path')
    this.veilPath.setAttribute('class', 'ze-guide__veil')
    this.veilPath.setAttribute('fill-rule', 'evenodd')
    this.veilPath.style.opacity = '0'
    this.washPath = document.createElementNS('http://www.w3.org/2000/svg', 'path')
    this.washPath.setAttribute('class', 'ze-guide__wash')
    this.washPath.style.opacity = '0'
    this.shape = document.createElementNS('http://www.w3.org/2000/svg', 'path')
    this.shape.setAttribute('class', 'ze-guide__shape')
    this.shape.setAttribute('fill-rule', 'evenodd')
    this.svg.append(this.veilPath, this.washPath, this.shape)
    this.probe = document.createElement('span')
    this.probe.style.display = 'none'
    host.append(this.cv, this.glow, this.svg, this.probe)
    this.setShapePath(CIRCLE)
    this.resize()
    window.addEventListener('resize', this.resize)
    this.refreshColors()
    this.hide()
  }

  destroy(): void {
    cancelAnimationFrame(this.raf)
    if (this.pendingTimer) clearTimeout(this.pendingTimer)
    window.removeEventListener('resize', this.resize)
    this.cv.remove(); this.glow.remove(); this.svg.remove(); this.probe.remove()
  }

  /** GuideLayer 每次渲染喂入声明式输入;引擎自行 reconcile 成命令式动作。 */
  update(input: SpriteInput): void {
    this.input = input
    const { mode, rect } = input
    if (mode === 'hidden') {
      if (this.visible) this.hide()
      this.destKey = 'hidden'
      return
    }
    if (mode === 'dismiss') {
      if (this.destKey !== 'dismiss' && this.visible) {
        this.destKey = 'dismiss'
        this.clearPending()
        this.unframeIfNeeded(() => {
          this.morph(0)
          const anim = motionOff()
            ? null
            : this.svg.animate(
                [{ transform: this.svg.style.transform, opacity: 1 }, { opacity: 0 }],
                { duration: P.exitDur, easing: 'ease-in', fill: 'forwards' },
              )
          const done = () => this.hide()
          if (anim) anim.onfinish = done
          else done()
        })
      }
      return
    }

    // 可见目标态:rest / point / act / frame
    if (!this.visible) this.show(parkPoint(mode, rect, input.rest))

    const key = mode === 'frame'
      ? 'frame'
      : mode === 'rest'
        ? `rest:${Math.round(input.rest.x / 8)}:${Math.round(input.rest.y / 8)}`
        : `${mode}:${rect ? `${Math.round((rect.left + rect.width / 2) / 12)}:${Math.round(rect.top / 12)}` : 'none'}`
    if (key === this.destKey) return
    this.destKey = key
    this.clearPending()

    if (mode === 'rest' || !rect) {
      this.unframeIfNeeded(() => {
        this.flyTo(input.rest.x, input.rest.y, () => { this.morph(0); this.rotT = IDLE_ROT })
      })
      return
    }
    if (mode === 'point') {
      const park = parkPoint('point', rect, input.rest)
      this.unframeIfNeeded(() => {
        this.flyTo(park.x, park.y, () => { this.morph(1); this.rotT = IDLE_ROT })
      })
      return
    }
    if (mode === 'panel') {
      // §7.3:停靠到面板旁即回**圆球静息态**(morph 0)——面板态里主要信息在可读面板中,
      // 精灵只提供存在感与归属指向,不做 point/act 那样的指点形变与到达特效。
      const park = parkPoint('panel', rect, input.rest)
      this.unframeIfNeeded(() => {
        this.flyTo(park.x, park.y, () => { this.morph(0); this.rotT = IDLE_ROT })
      })
      return
    }
    if (mode === 'act') {
      const cx = rect.left + rect.width / 2
      const cy = rect.top + rect.height / 2
      const park = parkPoint('act', rect, input.rest)
      this.unframeIfNeeded(() => {
        this.flyTo(cx, cy, () => {
          this.arriveFx()
          this.morph(1)
          this.rotT = IDLE_ROT
          // 点击停留一个可读节拍(1.25s),再收到目标上方悬停指着它(持续 act 步语义)。
          this.pendingTimer = setTimeout(() => {
            this.pendingTimer = null
            this.flyTo(park.x, park.y, () => { this.morph(1); this.rotT = IDLE_ROT })
          }, motionOff() ? 0 : P.clickHold)
        })
      })
      return
    }
    // frame
    const fcx = rect.left + rect.width / 2
    const fcy = rect.top + rect.height / 2
    this.unframeIfNeeded(() => {
      this.flyTo(fcx, fcy, () => this.frameIn())
    })
  }

  /* ── 可见性 ─────────────────────────────────────────────── */
  private show(at: { x: number; y: number }): void {
    this.visible = true
    this.svg.style.display = ''
    this.glow.style.display = ''
    this.cv.style.display = ''
    this.pose = { x: at.x - HALF, y: at.y - HALF, w: SPRITE_PX, h: SPRITE_PX }
    this.form = 0
    this.morphFrom = CIRCLE; this.morphTo = CIRCLE; this.morphing = false
    this.setShapePath(CIRCLE)
    this.svg.setAttribute('viewBox', `0 0 ${VB} ${VB}`)
    this.rot = IDLE_ROT; this.rotT = IDLE_ROT
    this.squash = 1
    this.trail = []
    if (!motionOff()) {
      this.svg.animate(
        [{ transform: `rotate(${IDLE_ROT}deg) scale(0.4)`, opacity: 0 }, { opacity: 1 }],
        { duration: P.appearDur, easing: 'cubic-bezier(0.34, 1.42, 0.5, 1)' },
      )
    }
    this.lastT = performance.now()
    cancelAnimationFrame(this.raf)
    this.raf = requestAnimationFrame(this.tickFrame)
  }

  private hide(): void {
    this.visible = false
    cancelAnimationFrame(this.raf)
    this.raf = 0
    this.clearPending()
    this.flying = false; this.flight = null; this.arriveCb = null
    this.framed = false; this.frameAnim = null
    this.trail = []
    if (this.ctx) this.ctx.clearRect(0, 0, this.cv.width, this.cv.height)
    this.svg.getAnimations().forEach((a) => a.cancel())
    this.svg.style.display = 'none'
    this.glow.style.display = 'none'
    this.cv.style.display = 'none'
    this.veilPath.setAttribute('d', ''); this.veilPath.style.opacity = '0'
    this.washPath.setAttribute('d', ''); this.washPath.style.opacity = '0'
    this.shape.style.filter = ''
  }

  private clearPending(): void {
    if (this.pendingTimer) { clearTimeout(this.pendingTimer); this.pendingTimer = null }
  }

  /* ── 变形(球⇄箭头逐点插值;照搬) ───────────────────────── */
  private setShapePath(pts: Pt[]): void {
    this.shape.setAttribute('d', pathD(pts))
  }

  private curPts(): Pt[] {
    const t = this.morphing ? Math.min(1, (performance.now() - this.morphT0) / P.morphDur) : 1
    const e = easeInOutCubic(t)
    const pts: Pt[] = []
    for (let i = 0; i < N; i++) {
      pts.push([
        this.morphFrom[i][0] + (this.morphTo[i][0] - this.morphFrom[i][0]) * e,
        this.morphFrom[i][1] + (this.morphTo[i][1] - this.morphFrom[i][1]) * e,
      ])
    }
    return pts
  }

  private morph(toForm: 0 | 1): void {
    if (toForm === this.form && !this.morphing) return
    if (motionOff()) {
      this.form = toForm
      this.morphFrom = this.morphTo = toForm === 0 ? CIRCLE : ARROW
      this.morphing = false
      this.setShapePath(this.morphTo)
      return
    }
    this.morphFrom = this.curPts()
    this.morphTo = toForm === 0 ? CIRCLE : ARROW
    this.morphT0 = performance.now()
    this.morphing = true
    this.form = toForm
  }

  /* ── 飞行(贝塞尔弧线 + 惯性转弯;照搬) ─────────────────── */
  private center(): { x: number; y: number } {
    return { x: this.pose.x + this.pose.w / 2, y: this.pose.y + this.pose.h / 2 }
  }

  private flyTo(x: number, y: number, onArrive?: () => void): void {
    if (motionOff()) {
      this.pose = { x: x - HALF, y: y - HALF, w: SPRITE_PX, h: SPRITE_PX }
      this.flying = false; this.flight = null
      if (onArrive) onArrive()
      return
    }
    const c = this.center()
    const dist = Math.hypot(x - c.x, y - c.y)
    if (dist < 2 && !this.flying) { if (onArrive) onArrive(); return }
    this.morph(0)      // 移动保持球体(带果冻形变),不变光标
    const retarget = this.flying && !!this.flight
    const start = () => {
      const c0 = this.center()
      const dx = x - c0.x, dy = y - c0.y, d = Math.hypot(dx, dy)
      if (retarget && this.flight) {
        // 惯性转弯:新曲线出切点沿当前速度方向,速度连续不折线
        const sp = Math.hypot(this.velNow.x, this.velNow.y) || 1
        const lead = Math.min(d * 0.45, sp * 0.35) * P.turnLead
        this.flight = {
          x0: c0.x, y0: c0.y,
          cx: c0.x + (this.velNow.x / sp) * lead, cy: c0.y + (this.velNow.y / sp) * lead,
          x1: x, y1: y,
          t0: performance.now(), dur: Math.min(1350, Math.max(380, (d * 0.72) / P.speed)),
          wob: this.flight.wob,
        }
        return
      }
      if (d < 130) {
        this.flight = {
          x0: c0.x, y0: c0.y, cx: (c0.x + x) / 2, cy: (c0.y + y) / 2, x1: x, y1: y,
          t0: performance.now(), dur: Math.max(300, (d * 1.1) / P.speed), wob: 0,
        }
      } else {
        const side = Math.random() < 0.5 ? -1 : 1
        const bow = d * (0.10 + Math.random() * 0.10) * P.bow * side
        this.flight = {
          x0: c0.x, y0: c0.y,
          cx: (c0.x + x) / 2 + (-dy / d) * bow, cy: (c0.y + y) / 2 + (dx / d) * bow,
          x1: x, y1: y,
          t0: performance.now(), dur: Math.min(1350, Math.max(480, (d * 0.78) / P.speed)),
          wob: (Math.random() * 0.6 + 0.4) * side * P.wobble,
        }
      }
      this.flying = true
    }
    this.arriveCb = onArrive ?? null
    if (retarget) start()
    else setTimeout(start, 120)   // 变形起步节拍(照搬)
  }

  /* ── 圈选(本体长成环形矩形;照搬) ─────────────────────── */
  private frameBox(rect: SpriteRectPx): Pose {
    return {
      x: rect.left - P.framePad, y: rect.top - P.framePad,
      w: rect.width + P.framePad * 2, h: rect.height + P.framePad * 2,
    }
  }

  private frameIn(): void {
    const rect = this.input.rect
    if (!rect) return
    this.rotT = 0
    if (motionOff()) {
      this.framed = true
      this.frameAnim = null
      this.pose = this.frameBox(rect)
      return
    }
    this.frameAnim = {
      dir: 'in', t0: performance.now(), dur: P.frameInDur,
      fromPts: this.curPts(), fromBox: { ...this.pose }, toBox: this.frameBox(rect),
    }
  }

  private unframeIfNeeded(cb: () => void): void {
    if (!this.framed && !this.frameAnim) { cb(); return }
    if (motionOff()) {
      this.framed = false; this.frameAnim = null
      const c = this.center()
      this.pose = { x: c.x - HALF, y: c.y - HALF, w: SPRITE_PX, h: SPRITE_PX }
      this.svg.setAttribute('viewBox', `0 0 ${VB} ${VB}`)
      this.veilPath.setAttribute('d', ''); this.veilPath.style.opacity = '0'
      this.washPath.setAttribute('d', ''); this.washPath.style.opacity = '0'
      this.shape.style.filter = ''
      this.form = 0; this.morphFrom = CIRCLE; this.morphTo = CIRCLE; this.morphing = false
      this.setShapePath(CIRCLE)
      this.rotT = IDLE_ROT
      cb()
      return
    }
    const ring = ringPts(this.pose.w, this.pose.h)
    this.frameAnim = {
      dir: 'out', t0: performance.now(), dur: P.frameOutDur,
      fromPts: [...ring.outer, ...ring.inner],
      fromBox: { ...this.pose },
      toBox: {
        x: this.pose.x + this.pose.w / 2 - HALF, y: this.pose.y + this.pose.h / 2 - HALF,
        w: SPRITE_PX, h: SPRITE_PX,
      },
    }
    this.framed = false
    // 出动画由 frameAnim 驱动,防与 morph 系统打架(照搬)
    this.form = 0; this.morphFrom = CIRCLE; this.morphTo = CIRCLE; this.morphing = false
    this.rotT = IDLE_ROT
    this.shape.style.filter = ''
    setTimeout(cb, 300)
  }

  private stepFrameAnim(now: number): void {
    const a = this.frameAnim
    if (!a) return
    const t = Math.min(1, (now - a.t0) / a.dur)
    const e = a.dir === 'in' ? easeOutBack(t) : easeInOutCubic(t)
    this.pose = {
      x: a.fromBox.x + (a.toBox.x - a.fromBox.x) * e,
      y: a.fromBox.y + (a.toBox.y - a.fromBox.y) * e,
      w: a.fromBox.w + (a.toBox.w - a.fromBox.w) * e,
      h: a.fromBox.h + (a.toBox.h - a.fromBox.h) * e,
    }
    this.svg.setAttribute('viewBox', `0 0 ${this.pose.w} ${this.pose.h}`)
    const ring = ringPts(this.pose.w, this.pose.h)
    const target: Pt[] = a.dir === 'in' ? [...ring.outer, ...ring.inner] : CIRCLE
    const pts: Pt[] = []
    for (let i = 0; i < N; i++) {
      pts.push([
        a.fromPts[i][0] + (target[i][0] - a.fromPts[i][0]) * e,
        a.fromPts[i][1] + (target[i][1] - a.fromPts[i][1]) * e,
      ])
    }
    const outer = pts.slice(0, 16)
    const inner = pts.slice(16)
    this.shape.setAttribute('d', pathD(outer) + ' ' + pathD(inner))
    this.paintFrameFx(this.pose, pathD(outer), pathD(inner), a.dir === 'in' ? e : 1 - e)
    if (t >= 1) {
      if (a.dir === 'in') {
        this.framed = true
      } else {
        this.veilPath.setAttribute('d', ''); this.veilPath.style.opacity = '0'
        this.washPath.setAttribute('d', ''); this.washPath.style.opacity = '0'
        this.svg.setAttribute('viewBox', `0 0 ${VB} ${VB}`)
        this.setShapePath(CIRCLE)
        this.shape.style.filter = ''
      }
      this.frameAnim = null
    }
  }

  private stepFramed(): void {
    const rect = this.input.rect
    if (!rect) return
    const target = this.frameBox(rect)
    const follow = motionOff() ? 1 : 0.2
    this.pose = {
      x: this.pose.x + (target.x - this.pose.x) * follow,
      y: this.pose.y + (target.y - this.pose.y) * follow,
      w: this.pose.w + (target.w - this.pose.w) * follow,
      h: this.pose.h + (target.h - this.pose.h) * follow,
    }
    this.svg.setAttribute('viewBox', `0 0 ${this.pose.w} ${this.pose.h}`)
    const ring = ringPts(this.pose.w, this.pose.h)
    this.shape.setAttribute('d', ring.outerD + ' ' + ring.innerD)
    this.paintFrameFx(this.pose, ring.outerD, ring.innerD, 1)
  }

  /** 圈选特效:环外压暗(evenodd 挖空) + 环内 wash + 沿环发光。 */
  private paintFrameFx(pose: Pose, outerD: string, innerD: string, alpha: number): void {
    const v = 4000
    this.veilPath.setAttribute('d', `M ${-v} ${-v} H ${pose.w + v} V ${pose.h + v} H ${-v} Z ` + outerD)
    this.veilPath.style.opacity = String(alpha)
    this.washPath.setAttribute('d', innerD)
    this.washPath.style.opacity = String(alpha)
    // 沿环发光走令牌(--ze-guide-frame-glow 已含定稿参数 10×frameGlow+4px)
    this.shape.style.filter = motionOff() ? '' : 'var(--ze-guide-frame-glow)'
  }

  /* ── 到点特效:涟漪 + 落地回弹 + tick(照搬) ─────────────── */
  private arriveFx(): void {
    if (motionOff()) return
    const c = this.center()
    const r = document.createElement('i')
    r.className = 'ze-guide__rip'
    r.style.left = `${c.x}px`
    r.style.top = `${c.y}px`
    this.host.appendChild(r)
    setTimeout(() => r.remove(), 600)
    this.svg.animate(
      [{ scale: '0.82' }, { scale: '1.12', offset: 0.55 }, { scale: '1' }],
      { duration: 300, easing: 'cubic-bezier(0.34, 1.42, 0.5, 1)' },
    )
    this.tickSound()
  }

  private tickSound(): void {
    if (!this.input.sound) return
    try {
      type ACWindow = Window & { webkitAudioContext?: typeof AudioContext }
      const Ctor = window.AudioContext ?? (window as ACWindow).webkitAudioContext
      if (!Ctor) return
      this.ac = this.ac ?? new Ctor()
      const o = this.ac.createOscillator()
      const g = this.ac.createGain()
      o.type = 'sine'
      o.frequency.value = 1560
      g.gain.setValueAtTime(0.0001, this.ac.currentTime)
      g.gain.exponentialRampToValueAtTime(0.05, this.ac.currentTime + 0.008)
      g.gain.exponentialRampToValueAtTime(0.0001, this.ac.currentTime + 0.06)
      o.connect(g).connect(this.ac.destination)
      o.start()
      o.stop(this.ac.currentTime + 0.07)
    } catch { /* 音频不可用则静默 */ }
  }

  /* ── 颜色探针(canvas 需要具体色;随主题/accent 刷新) ───── */
  private refreshColors(): void {
    this.probe.style.color = 'var(--ze-accent-500)'
    const accent = getComputedStyle(this.probe).color
    if (accent) this.accentColor = accent
    this.probe.style.color = 'var(--ze-guide-trail-hi)'
    const hi = getComputedStyle(this.probe).color
    if (hi) this.hiColor = hi
  }

  private withAlpha(color: string, a: number): string {
    if (color.startsWith('rgb(')) return color.replace('rgb(', 'rgba(').replace(')', `, ${a})`)
    if (color.startsWith('color(') || color.startsWith('oklch(') || color.startsWith('oklab(')) {
      return color.replace(/\)\s*$/, ` / ${a})`)
    }
    return color
  }

  /* ── 主循环(照搬结构) ──────────────────────────────────── */
  private resize = (): void => {
    const d = window.devicePixelRatio || 1
    const w = window.innerWidth
    const h = window.innerHeight
    this.cv.width = w * d
    this.cv.height = h * d
    this.cv.style.width = `${w}px`
    this.cv.style.height = `${h}px`
    if (this.ctx) this.ctx.setTransform(d, 0, 0, d, 0, 0)
  }

  private tickFrame = (now: number): void => {
    if (!this.visible) return
    const dt = Math.min(50, now - this.lastT)
    this.lastT = now
    this.frameCount++
    if (this.frameCount % 60 === 0) this.refreshColors()
    const reduced = motionOff()

    if (this.frameAnim) this.stepFrameAnim(now)
    else if (this.framed) this.stepFramed()
    else if (this.morphing) {
      this.setShapePath(this.curPts())
      if ((now - this.morphT0) / P.morphDur >= 1) {
        this.morphing = false
        this.morphFrom = this.morphTo
      }
    }

    if (this.flying && this.flight) {
      const f = this.flight
      const t = Math.min(1, (now - f.t0) / f.dur)
      const e = easeInOutCubic(t)
      const u = 1 - e
      let bx = u * u * f.x0 + 2 * u * e * f.cx + e * e * f.x1
      let by = u * u * f.y0 + 2 * u * e * f.cy + e * e * f.y1
      if (f.wob) {
        const nx = f.y1 - f.y0
        const ny = -(f.x1 - f.x0)
        const nl = Math.hypot(nx, ny) || 1
        const w = f.wob * Math.sin(t * Math.PI * 2.2) * (1 - t) * 9
        bx += (nx / nl) * w
        by += (ny / nl) * w
      }
      const c0 = this.center()
      this.velNow = { x: ((bx - c0.x) / Math.max(1, dt)) * 1000, y: ((by - c0.y) / Math.max(1, dt)) * 1000 }
      this.rotT = (Math.atan2(by - c0.y, bx - c0.x) * 180) / Math.PI + 90
      // 果冻拉伸:沿速度方向,速度越快拉越长(上限照搬)
      const speed = Math.hypot(this.velNow.x, this.velNow.y)
      this.squash += (1 + Math.min(P.squashMax - 1, speed / 3200) - this.squash) * 0.22
      const last = this.trail[this.trail.length - 1]
      if (!last || Math.hypot(bx - last.x, by - last.y) > 1.5) this.trail.push({ x: bx, y: by, t: now })
      this.pose = { x: bx - HALF, y: by - HALF, w: SPRITE_PX, h: SPRITE_PX }
      if (t >= 1) {
        this.flying = false
        this.flight = null
        this.rotT = IDLE_ROT
        const done = this.arriveCb
        this.arriveCb = null
        if (done) done()
      }
    }

    this.rot += (this.rotT - this.rot) * 0.12
    if (!this.flying) this.squash += (1 - this.squash) * 0.14

    const breatheDur = this.input.thinking ? 1400 : P.breatheDur
    const breathe = this.form === 0 && !this.morphing && !this.framed && !this.frameAnim && !reduced
      ? 1 + P.breatheAmp * Math.sin((now / breatheDur) * Math.PI * 2)
      : 1
    const sX = this.flying ? this.squash : breathe
    const sY = this.flying ? 1 / Math.sqrt(this.squash) : breathe
    this.svg.style.left = `${this.pose.x}px`
    this.svg.style.top = `${this.pose.y}px`
    this.svg.style.width = `${this.pose.w}px`
    this.svg.style.height = `${this.pose.h}px`
    this.svg.style.transform = this.framed || this.frameAnim
      ? `rotate(${this.rot}deg)`
      : `rotate(${this.rot}deg) scale(${sX}, ${sY})`

    // 待机球柔光(只给球;圈选/飞行/箭头态都关)
    const cc = this.center()
    const orbOn = this.form === 0 && !this.framed && !this.frameAnim && !reduced
    this.glow.style.opacity = orbOn
      ? String(0.32 + 0.1 * Math.sin((now / breatheDur) * Math.PI * 2))
      : '0'
    this.glow.style.transform = `translate(${cc.x - 45}px, ${cc.y - 45}px)`

    // 余晖:整帧重建,按时长精确消散零残留
    this.trail = reduced ? [] : this.trail.filter((p) => now - p.t < P.trailAge)
    const ctx = this.ctx
    if (ctx) {
      ctx.clearRect(0, 0, window.innerWidth, window.innerHeight)
      if (this.trail.length > 1) {
        const dark = document.documentElement.dataset.theme === 'dark'
        ctx.lineCap = 'round'
        ctx.lineJoin = 'round'
        for (let pass = 0; pass < 2; pass++) {
          for (let i = 1; i < this.trail.length; i++) {
            const a0 = this.trail[i - 1]
            const a1 = this.trail[i]
            const fr = i / this.trail.length
            const age = 1 - (now - a1.t) / P.trailAge
            const w = (pass === 0 ? 18 : 8) * P.trailW * fr * fr * age
            if (w < 0.3) continue
            ctx.strokeStyle = pass === 0 ? this.accentColor : this.hiColor
            ctx.globalAlpha = (pass === 0 ? 0.2 : 0.55) * P.trailA * fr * age * (dark ? 1 : 0.75)
            ctx.lineWidth = w
            ctx.beginPath()
            ctx.moveTo(a0.x, a0.y)
            ctx.lineTo(a1.x, a1.y)
            ctx.stroke()
          }
        }
        const head = this.trail[this.trail.length - 1]
        const g = ctx.createRadialGradient(head.x, head.y, 0, head.x, head.y, 24)
        g.addColorStop(0, this.withAlpha(this.accentColor, dark ? 0.5 : 0.3))
        g.addColorStop(1, this.withAlpha(this.accentColor, 0))
        ctx.globalAlpha = 1
        ctx.fillStyle = g
        ctx.beginPath()
        ctx.arc(head.x, head.y, 24, 0, Math.PI * 2)
        ctx.fill()
      }
    }

    this.raf = requestAnimationFrame(this.tickFrame)
  }
}
