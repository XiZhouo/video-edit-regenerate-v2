/**
 * useViewport — 视口缩放/平移 state(框架级)
 *
 * zoom-to-cursor(光标下图像点不动)+ 中键 pan + fit-contain;数学全在 spatialMath(纯、已测)。
 * viewportRef 给指针处理器读最新值,避免 stale 闭包(沿用 CanvasViewport 的 offRef 技巧)。
 */

import { useCallback, useMemo, useRef } from 'react'
import {
  useScopedViewState, type ViewStateScope, type ViewStateSliceCodec,
} from '@ze/core/04-user-facing/05-preferences'
import { fitViewport, zoomAt as zoomAtMath } from './spatialMath'
import type { Vec2, Viewport } from './spatialTypes'

export type UseViewportOpts = {
  minScale?: number; maxScale?: number; zoomStep?: number
  /** 共享 per-user × per-skill × per-project × per-slot 视图态作用域。 */
  viewStateScope?: ViewStateScope
  /** @deprecated 旧 localStorage key;无 viewStateScope 时仅作兼容降级。 */
  storageKey?: string
}

export type UseViewport = {
  viewport: Viewport
  viewportRef: React.MutableRefObject<Viewport>
  zoomAt: (containerPoint: Vec2, deltaY: number) => void
  panBy: (d: Vec2) => void
  fit: (content: { w: number; h: number }, container: { w: number; h: number }) => void
  /** 首次打开自适应:只改当前显示,不写成用户偏好。 */
  fitTransient: (content: { w: number; h: number }, container: { w: number; h: number }) => void
  setViewport: (v: Viewport) => void
  /** 打开时是否已从 storageKey 恢复过视口(true=有缓存,caller 据此决定是否 auto-fit)。 */
  hasStored: boolean
  /** scope/slice 稳定身份;consumer 用它重置每个作用域的首开初始化。 */
  preferenceIdentity: string
}

export const CANVAS2D_VIEWPORT_SLICE = 'canvas2d.spatial.viewport'
const DEFAULT_VIEWPORT: Viewport = { scale: 1, tx: 0, ty: 0 }

function decodeViewport(value: unknown): Viewport | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const rec = value as Record<string, unknown>
  const { scale, tx, ty } = rec
  return typeof scale === 'number' && scale > 0 && Number.isFinite(scale)
    && typeof tx === 'number' && Number.isFinite(tx)
    && typeof ty === 'number' && Number.isFinite(ty)
    ? { scale, tx, ty }
    : null
}

export const CANVAS2D_VIEWPORT_CODEC: ViewStateSliceCodec<Viewport> = {
  version: 1,
  decode: decodeViewport,
  migrate: (value, fromVersion) => {
    if (fromVersion !== 0 || !value || typeof value !== 'object' || Array.isArray(value)) return null
    const rec = value as Record<string, unknown>
    return decodeViewport({
      scale: rec.scale ?? rec.zoom,
      tx: rec.tx ?? rec.x,
      ty: rec.ty ?? rec.y,
    })
  },
}

/** 从 localStorage 读持久化视口;无 key / 无值 / 损坏 / 私有模式均返 null(视作无缓存)。 */
function readStoredViewport(key: string | undefined): Viewport | null {
  if (!key || typeof localStorage === 'undefined') return null
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const o = JSON.parse(raw) as Partial<Viewport>
    if (o && typeof o.scale === 'number' && o.scale > 0 &&
        typeof o.tx === 'number' && typeof o.ty === 'number' &&
        Number.isFinite(o.scale) && Number.isFinite(o.tx) && Number.isFinite(o.ty)) {
      return { scale: o.scale, tx: o.tx, ty: o.ty }
    }
  } catch { /* 损坏/私有模式 → 视作无缓存 */ }
  return null
}

function writeStoredViewport(key: string | undefined, v: Viewport): void {
  if (!key || typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(key, JSON.stringify({ scale: v.scale, tx: v.tx, ty: v.ty }))
  } catch { /* 私有模式 localStorage 不可用 → 静默降级为内存态 */ }
}

export function useViewport(opts: UseViewportOpts = {}): UseViewport {
  const minScale = opts.minScale ?? 0.2
  const maxScale = opts.maxScale ?? 12
  const step = opts.zoomStep ?? 1.1
  const storageKey = opts.storageKey
  const legacyStored = opts.viewStateScope ? null : readStoredViewport(storageKey)
  const defaultViewport = useMemo<Viewport>(
    () => legacyStored ?? DEFAULT_VIEWPORT,
    [legacyStored?.scale, legacyStored?.tx, legacyStored?.ty],
  )
  const state = useScopedViewState({
    scope: opts.viewStateScope,
    slice: opts.viewStateScope
      ? CANVAS2D_VIEWPORT_SLICE
      : `${CANVAS2D_VIEWPORT_SLICE}:legacy:${storageKey ?? 'memory'}`,
    codec: CANVAS2D_VIEWPORT_CODEC,
    defaultValue: defaultViewport,
  })
  const viewport = state.value
  const viewportRef = useRef<Viewport>(viewport)
  // scope 切换的同一 render 就让指针跟上新作用域,禁止事件闭包用旧项目视口计算。
  viewportRef.current = viewport
  const set = useCallback((v: Viewport, persist: boolean) => {
    viewportRef.current = v
    state.setValue(v, { persist: !!opts.viewStateScope && persist })
    if (!opts.viewStateScope && persist) writeStoredViewport(storageKey, v)
  }, [opts.viewStateScope, state.setValue, storageKey])

  const setPersistent = useCallback((v: Viewport) => set(v, true), [set])
  const setTransient = useCallback((v: Viewport) => set(v, false), [set])

  const zoomAt = useCallback((containerPoint: Vec2, deltaY: number) => {
    const factor = deltaY < 0 ? step : 1 / step
    setPersistent(zoomAtMath(viewportRef.current, containerPoint, factor, minScale, maxScale))
  }, [setPersistent, step, minScale, maxScale])

  const panBy = useCallback((d: Vec2) => {
    const v = viewportRef.current
    setPersistent({ ...v, tx: v.tx + d.x, ty: v.ty + d.y })
  }, [setPersistent])

  const fit = useCallback((content: { w: number; h: number }, container: { w: number; h: number }) => {
    setPersistent(fitViewport(content, container))
  }, [setPersistent])

  const fitTransient = useCallback((content: { w: number; h: number }, container: { w: number; h: number }) => {
    setTransient(fitViewport(content, container))
  }, [setTransient])

  return {
    viewport, viewportRef, zoomAt, panBy, fit, fitTransient, setViewport: setPersistent,
    hasStored: opts.viewStateScope ? state.hasStored : legacyStored !== null,
    preferenceIdentity: state.identity,
  }
}
