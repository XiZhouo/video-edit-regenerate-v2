/**
 * clipGeometry — 一层素材怎么摆进画布(界面这一侧)。
 *
 * **这是域层 `_domain/clip_geometry.py` 的镜像**:界面上画的变换手柄必须落在画面
 * 真正占的那块地方,而"真正占哪块"是渲染时 ffmpeg 算的。两边算错一点,人拖的框
 * 和出来的画面就对不上 —— 而这种错只有导出之后才看得见,极难倒查。
 *
 * 两边由一张钉子表钉着:`__fixtures__/clip_geometry_cases.json`。
 * Python 侧 `test_clip_geometry_fixture.py` 断言域层算出来等于表;
 * 这一侧 `clipGeometry.test.ts` 断言本文件算出来等于同一张表。
 * **改任何一侧的算式,两条测试必红一条** —— 这就是它们不许各走各的保证。
 */

export type ClipGeom = {
  fitMode?: string
  x?: number
  y?: number
  scale?: number
  cropL?: number
  cropT?: number
  cropR?: number
  cropB?: number
}

export type Rect = { x: number; y: number; width: number; height: number }

export const FIT_MODES = ['contain', 'cover', 'stretch', 'none'] as const
export const DEFAULT_FIT = 'contain'

const num = (v: any, d = 0): number => (Number.isFinite(Number(v)) ? Number(v) : d)

/** 零散字段 → 规整几何;缺省即老行为(等比装进去、居中、不裁、不缩放)。 */
export function normalizeGeometry(clip: ClipGeom | null | undefined) {
  const c = clip ?? {}
  const fit = String(c.fitMode || DEFAULT_FIT)
  return {
    fitMode: (FIT_MODES as readonly string[]).includes(fit) ? fit : DEFAULT_FIT,
    x: num(c.x), y: num(c.y),
    scale: Math.max(0.01, c.scale == null ? 1 : num(c.scale, 1)),
    cropL: Math.max(0, num(c.cropL)), cropT: Math.max(0, num(c.cropT)),
    cropR: Math.max(0, num(c.cropR)), cropB: Math.max(0, num(c.cropB)),
  }
}

/** 是不是「默认摆法」—— 是的话「复位」按钮该是灰的。 */
export function isIdentity(clip: ClipGeom | null | undefined): boolean {
  const g = normalizeGeometry(clip)
  return g.fitMode === DEFAULT_FIT && g.x === 0 && g.y === 0 && g.scale === 1
    && g.cropL === 0 && g.cropT === 0 && g.cropR === 0 && g.cropB === 0
}

/**
 * 这一层的画面落在画布的哪个矩形上(画布像素)。
 * 与域层 `canvas_rect_of` 同一套算式 —— 改这里必须同时改那里。
 */
export function canvasRectOf(clip: ClipGeom | null | undefined,
                             opts: { width: number; height: number;
                                     srcW: number; srcH: number }): Rect {
  const g = normalizeGeometry(clip)
  const sw = Math.max(1, num(opts.srcW) * (1 - g.cropL - g.cropR))
  const sh = Math.max(1, num(opts.srcH) * (1 - g.cropT - g.cropB))
  const w = num(opts.width), h = num(opts.height), s = g.scale

  let dw: number, dh: number
  if (g.fitMode === 'stretch') {
    dw = w; dh = h
  } else if (g.fitMode === 'none') {
    dw = sw * s; dh = sh * s
  } else {
    const ratio = g.fitMode === 'contain'
      ? Math.min(w * s / sw, h * s / sh)
      : Math.max(w * s / sw, h * s / sh)
    dw = sw * ratio; dh = sh * ratio
  }

  return { x: (w - dw) / 2 + g.x * w, y: (h - dh) / 2 + g.y * h, width: dw, height: dh }
}

/** 画布像素矩形 → 舞台百分比(界面画手柄用的坐标)。 */
export function rectToPercent(r: Rect, canvasW: number, canvasH: number): Rect {
  const w = canvasW || 1, h = canvasH || 1
  return { x: (r.x / w) * 100, y: (r.y / h) * 100,
           width: (r.width / w) * 100, height: (r.height / h) * 100 }
}
