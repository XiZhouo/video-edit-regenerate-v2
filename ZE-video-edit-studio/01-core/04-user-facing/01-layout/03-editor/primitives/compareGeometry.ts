/**
 * compareFrame — A/B 前后对比的**纯逻辑层**(W4;框架级,DOM 无关)
 *
 * 三态对比:并排 / 滑块分隔 / 叠加(onion)。此处只放可单测的几何/约束纯函数;
 * 渲染(图片 + 拖拽分隔条 + 不透明滑块)在 CompareFrame.tsx。
 */

export type CompareMode = 'side-by-side' | 'slider' | 'onion'

export const COMPARE_MODES: readonly CompareMode[] = ['side-by-side', 'slider', 'onion'] as const

export function isCompareMode(v: unknown): v is CompareMode {
  return typeof v === 'string' && (COMPARE_MODES as readonly string[]).includes(v)
}

/** 把任意数夹到 0..1(NaN → 0)。 */
export function clampUnit(v: number): number {
  if (Number.isNaN(v)) return 0
  return Math.min(1, Math.max(0, v))
}

/**
 * slider 模式:分隔条在 split(0..1,从左起)。before 图铺满底层显示左半;
 * after 图叠在上层、裁掉左侧 split 部分 → 只露出右半。返回 after 图的 clip-path inset。
 */
export function afterClipInset(split: number): string {
  const s = clampUnit(split)
  return `inset(0 0 0 ${(s * 100).toFixed(4).replace(/\.?0+$/, '')}%)`
}

/** 把容器内的指针 x(相对容器左边、px)+ 容器宽(px)换算成 0..1 的 split。 */
export function pointerToSplit(offsetX: number, width: number): number {
  if (width <= 0) return 0
  return clampUnit(offsetX / width)
}
