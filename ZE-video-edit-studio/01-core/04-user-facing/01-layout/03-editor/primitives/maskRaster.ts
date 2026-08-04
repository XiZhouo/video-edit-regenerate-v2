/**
 * maskRaster — 光栅 mask 的**纯逻辑层**(W1a;框架级,DOM 无关)
 *
 * mask = 单通道 alpha 位图(0..255)。笔画(brush/eraser)经 applyStroke 累积进 alpha buffer;
 * alphaToRGBA / rgbaToAlpha 在「单通道 alpha」与「画布 RGBA 像素」间互转(组件用 canvas 渲染 +
 * seed-from-asset 解码;此处不碰 DOM,故可 vitest)。
 *
 * 设计红线(同 onStroke 哲学):core 只把笔画累积成 mask 位图,**不解释 mask 含义**
 * (去噪 / 引导 / SAM 等算法全留 mod)。组件经 onMaskCommit(pngBytes) 把整张 mask 吐给 mod。
 *
 * 不可变:applyStroke 不改输入 mask,克隆后改副本再返回(符合 coding-style 的 immutability)。
 */

export type MaskBuffer = {
  w: number
  h: number
  /** 长度 = w*h;每字节 = 该像素 alpha(0=空,255=实) */
  alpha: Uint8ClampedArray
}

export type MaskMode = 'brush' | 'eraser'
export type MaskPoint = { x: number; y: number }

export type StrokeOpts = {
  /** 笔刷半径(图像像素) */
  radius: number
  mode: MaskMode
  /** brush 落值(默认 255 全实;支持半透明笔刷可调) */
  value?: number
  /** 0..1 or 0..100; 1/100 = hard Photoshop-like edge, 0 = full falloff. */
  hardness?: number
  /** 0..1 or 0..100; global stroke opacity. */
  opacity?: number
  /** 0..1 or 0..100; per-stamp flow. */
  flow?: number
  /** Stamp spacing. <=1 means fraction of brush diameter; >1 means image pixels. */
  spacing?: number
}

export function createMask(w: number, h: number): MaskBuffer {
  const W = Math.max(0, Math.floor(w))
  const H = Math.max(0, Math.floor(h))
  return { w: W, h: H, alpha: new Uint8ClampedArray(W * H) }
}

export function cloneMask(m: MaskBuffer): MaskBuffer {
  return { w: m.w, h: m.h, alpha: new Uint8ClampedArray(m.alpha) }
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v
}

function toUnit(v: number | undefined, fallback: number): number {
  if (v === undefined || !Number.isFinite(v)) return fallback
  return clamp01(v > 1 ? v / 100 : v)
}

/** 在 alpha buffer 上盖一个 PS-like 圆形笔刷(就地改传入的 buffer —— 调用方应传副本)。 */
function stampDisk(
  alpha: Uint8ClampedArray, w: number, h: number,
  cx: number, cy: number, radius: number, value: number, erase: boolean, hardness: number,
): void {
  const r = Math.max(0.5, radius)
  const r2 = r * r
  const hard = clamp01(hardness)
  const inner = r * hard
  const falloff = Math.max(0.001, r - inner)
  const x0 = Math.max(0, Math.floor(cx - r))
  const x1 = Math.min(w - 1, Math.ceil(cx + r))
  const y0 = Math.max(0, Math.floor(cy - r))
  const y1 = Math.min(h - 1, Math.ceil(cy + r))
  for (let y = y0; y <= y1; y++) {
    for (let x = x0; x <= x1; x++) {
      const dx = x - cx
      const dy = y - cy
      const d2 = dx * dx + dy * dy
      if (d2 <= r2) {
        const d = Math.sqrt(d2)
        const strength = d <= inner ? 1 : clamp01(1 - ((d - inner) / falloff))
        const amount = Math.round(value * strength)
        const i = y * w + x
        alpha[i] = erase
          ? Math.max(0, Math.round(alpha[i] * (1 - strength)))
          : Math.max(alpha[i], amount)
      }
    }
  }
}

/**
 * 把一条笔画(连续点序列)累积进 mask:相邻点之间按 radius/2 步长插值盖圆盘,
 * 防快速移动留断点。返回**新** MaskBuffer(输入不变)。
 */
export function applyStroke(mask: MaskBuffer, points: MaskPoint[], opts: StrokeOpts): MaskBuffer {
  const out = cloneMask(mask)
  if (points.length === 0) return out
  const erase = opts.mode === 'eraser'
  const opacity = toUnit(opts.opacity, 1)
  const flow = toUnit(opts.flow, 1)
  const value = Math.max(0, Math.min(255, Math.round((opts.value ?? 255) * opacity * flow)))
  const radius = Math.max(0.5, opts.radius)
  const hardness = toUnit(opts.hardness, 1)
  const { alpha, w, h } = out

  if (points.length === 1) {
    stampDisk(alpha, w, h, points[0].x, points[0].y, radius, value, erase, hardness)
    return out
  }
  const spacing = opts.spacing === undefined
    ? radius / 2
    : opts.spacing <= 1
      ? radius * 2 * opts.spacing
      : opts.spacing
  const step = Math.max(1, spacing)
  for (let k = 1; k < points.length; k++) {
    const a = points[k - 1]
    const b = points[k]
    const dist = Math.hypot(b.x - a.x, b.y - a.y)
    const segs = Math.max(1, Math.ceil(dist / step))
    for (let s = 0; s <= segs; s++) {
      const t = s / segs
      stampDisk(alpha, w, h, a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, radius, value, erase, hardness)
    }
  }
  return out
}

/** alpha>threshold 的像素数(给统计 / 空判 / 测试用)。 */
export function countFilled(mask: MaskBuffer, threshold = 0): number {
  let n = 0
  const a = mask.alpha
  for (let i = 0; i < a.length; i++) if (a[i] > threshold) n++
  return n
}

export function isEmpty(mask: MaskBuffer): boolean {
  return countFilled(mask) === 0
}

/**
 * 单通道 alpha → 画布 RGBA(纯色 + 逐像素 alpha)。给组件 putImageData 渲染半透明叠加层。
 * rgb 默认中性灰,真实叠加色由组件按主题令牌注入。
 */
export function alphaToRGBA(mask: MaskBuffer, rgb: { r: number; g: number; b: number } = { r: 255, g: 64, b: 64 }): Uint8ClampedArray {
  const out = new Uint8ClampedArray(mask.w * mask.h * 4)
  const a = mask.alpha
  for (let i = 0; i < a.length; i++) {
    const j = i * 4
    out[j] = rgb.r
    out[j + 1] = rgb.g
    out[j + 2] = rgb.b
    out[j + 3] = a[i]
  }
  return out
}

/**
 * 画布 RGBA → 单通道 alpha(seed-from-asset:把已存 mask 图的 alpha 通道读回)。
 * 输入既可是带 alpha 的 mask PNG,也可是「白底涂抹」灰度图(alpha 全 255 时回退取亮度)。
 */
export function rgbaToAlpha(rgba: Uint8ClampedArray, w: number, h: number): MaskBuffer {
  const m = createMask(w, h)
  const n = w * h
  let hasTransparency = false   // 出现任一 alpha<255 → alpha 通道含真实 mask 信息
  for (let i = 0; i < n; i++) {
    const av = rgba[i * 4 + 3]
    m.alpha[i] = av
    if (av < 255) hasTransparency = true
  }
  // alpha 通道整体为满(不透明图,无透明信息,如白底灰度 mask)→ 回退用亮度作 alpha。
  if (!hasTransparency) {
    for (let i = 0; i < n; i++) {
      const j = i * 4
      m.alpha[i] = Math.round((rgba[j] + rgba[j + 1] + rgba[j + 2]) / 3)
    }
  }
  return m
}
