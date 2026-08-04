/**
 * useMaskEditor — 历史命名的像素层绘制状态/逻辑(从 MaskCanvas 抽出)
 *
 * 把「绘制位图/mode/笔刷/硬度 + 视图(scale/off/cursor) + 历史(undo/redo) + commit」
 * 全收进一个 hook,让**工具功能区**(MaskToolbarZone, 编辑器 chrome 的 zone)和**画布**(MaskCanvas,
 * 内容区)共享同一份状态 —— 工具栏不再内嵌在画布里、不再随底图有无而显隐。
 *
 * 返回:
 *   - controls:给工具功能区(mode/setMode、radius/setRadius、hardness/setHardness、undo/redo/clear、canUndo/canRedo)
 *   - canvas:  给画布(refs + 绘制 handlers + 视图态 + fitToView)
 *
 * 红线不变(同 MaskCanvas):core 只把笔画累积成位图,不解释含义;纯位图逻辑在 maskRaster.ts。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createMask, cloneMask, applyStroke, alphaToRGBA, rgbaToAlpha, isEmpty,
  type MaskBuffer, type MaskMode, type MaskPoint,
} from './maskRaster'
import { clampPointToSize, clientPointInElement, fitWorkspace, screenToWorld, zoomWorkspaceAtPoint } from './canvasWorkspace'

/** 撤销历史保留的最近笔画状态数上限(超出丢最旧;每张快照=整层位图克隆,故有界防内存膨胀)。 */
const UNDO_HISTORY_LIMIT = 24

export type UseMaskEditorProps = {
  src?: string
  maskSrc?: string
  brushSize?: number
  brushOpacity?: number
  brushFlow?: number
  brushSpacing?: number
  tint?: { r: number; g: number; b: number }
  onMaskCommit?: (pngBytes: Uint8Array) => void
  commitDebounceMs?: number
  disabled?: boolean
}

export type MaskControls = {
  mode: MaskMode
  setMode: (m: MaskMode) => void
  radius: number
  setRadius: (r: number) => void
  hardness: number
  setHardness: (h: number) => void
  undo: () => void
  redo: () => void
  clear: () => void
  canUndo: boolean
  canRedo: boolean
  disabled: boolean
}

export type MaskCanvasState = {
  src?: string
  mask: MaskBuffer | null
  mode: MaskMode
  radius: number
  hardness: number
  scale: number
  off: { x: number; y: number }
  cursor: MaskPoint | null
  stageRef: React.RefObject<HTMLDivElement | null>
  canvasRef: React.RefObject<HTMLCanvasElement | null>
  onPointerDown: (e: React.PointerEvent) => void
  onPointerMove: (e: React.PointerEvent) => void
  onPointerUp: (e: React.PointerEvent) => void
  onPointerLeave: () => void
  fitToView: (m?: MaskBuffer | null) => void
}

export function useMaskEditor({
  src, maskSrc, brushSize = 24, tint = { r: 255, g: 64, b: 64 },
  brushOpacity = 1, brushFlow = 1, brushSpacing,
  onMaskCommit, commitDebounceMs = 400, disabled = false,
}: UseMaskEditorProps): { controls: MaskControls; canvas: MaskCanvasState } {
  const [mask, setMask] = useState<MaskBuffer | null>(null)
  const [mode, setMode] = useState<MaskMode>('brush')
  const [radius, setRadius] = useState(Math.max(1, brushSize))
  const [hardness, setHardness] = useState(80)
  const [scale, setScale] = useState(1)
  const [off, setOff] = useState({ x: 0, y: 0 })
  const [cursor, setCursor] = useState<MaskPoint | null>(null)
  // canUndo/canRedo 用 state(随历史变重渲染工具功能区);buffer 仍走 ref(避免大数组进 state)。
  const [canUndo, setCanUndo] = useState(false)
  const [canRedo, setCanRedo] = useState(false)
  const stageRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drawing = useRef<{ last: MaskPoint } | null>(null)
  const panning = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null)
  const brushAdjust = useRef<{ x: number; y: number; r: number; h: number } | null>(null)
  const history = useRef<MaskBuffer[]>([])
  const redo = useRef<MaskBuffer[]>([])
  const lastFitSize = useRef('')
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const maskRef = useRef<MaskBuffer | null>(null)
  maskRef.current = mask
  const offRef = useRef(off)
  offRef.current = off
  const scaleRef = useRef(scale)
  scaleRef.current = scale
  const tintRef = useRef(tint)
  tintRef.current = tint
  const onCommitRef = useRef(onMaskCommit)
  onCommitRef.current = onMaskCommit

  const syncHistoryFlags = useCallback(() => {
    setCanUndo(history.current.length > 0)
    setCanRedo(redo.current.length > 0)
  }, [])

  const fitToView = useCallback((m: MaskBuffer | null = maskRef.current) => {
    const stage = stageRef.current
    if (!stage || !m || m.w <= 0 || m.h <= 0) return
    const rect = stage.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return
    const next = fitWorkspace({ w: rect.width, h: rect.height }, { w: m.w, h: m.h })
    setScale(next.scale)
    setOff(next.off)
  }, [])

  const pushHistory = useCallback(() => {
    const m = maskRef.current
    if (!m) return
    history.current = [...history.current.slice(-UNDO_HISTORY_LIMIT), cloneMask(m)]
    redo.current = []
    syncHistoryFlags()
  }, [syncHistoryFlags])

  // ---- 1. 底图加载 → 定 mask 尺寸 → seed(若有 maskSrc)----
  useEffect(() => {
    if (!src) { setMask(null); return }
    history.current = []
    redo.current = []
    syncHistoryFlags()
    lastFitSize.current = ''
    let alive = true
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => {
      if (!alive) return
      const w = img.naturalWidth || img.width
      const h = img.naturalHeight || img.height
      if (!maskSrc) { setMask(createMask(w, h)); return }
      const m = new Image()
      m.crossOrigin = 'anonymous'
      m.onload = () => {
        if (!alive) return
        try {
          const tmp = document.createElement('canvas')
          tmp.width = w; tmp.height = h
          const ctx = tmp.getContext('2d')
          if (!ctx) { setMask(createMask(w, h)); return }
          ctx.drawImage(m, 0, 0, w, h)
          const data = ctx.getImageData(0, 0, w, h).data
          setMask(rgbaToAlpha(data as unknown as Uint8ClampedArray, w, h))
        } catch {
          setMask(createMask(w, h))
        }
      }
      m.onerror = () => { if (alive) setMask(createMask(w, h)) }
      m.src = maskSrc
    }
    img.onerror = () => { if (alive) setMask(null) }
    img.src = src
    return () => { alive = false }
  }, [src, maskSrc, syncHistoryFlags])

  useEffect(() => {
    if (!mask) return
    const key = `${mask.w}x${mask.h}`
    if (lastFitSize.current === key) return
    lastFitSize.current = key
    window.requestAnimationFrame(() => fitToView(mask))
  }, [fitToView, mask])

  // ---- 2. mask 变 → 重绘叠加层 ----
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv || !mask) return
    if (cv.width !== mask.w) cv.width = mask.w
    if (cv.height !== mask.h) cv.height = mask.h
    const ctx = cv.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, mask.w, mask.h)
    if (!isEmpty(mask)) {
      const img = ctx.createImageData(mask.w, mask.h)
      img.data.set(alphaToRGBA(mask, tint))
      ctx.putImageData(img, 0, 0)
    }
  }, [mask, tint])

  // ---- 3. 卸载清防抖定时器 ----
  useEffect(() => () => { if (commitTimer.current) clearTimeout(commitTimer.current) }, [])

  useEffect(() => {
    const stage = stageRef.current
    if (!stage) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = stage.getBoundingClientRect()
      const next = zoomWorkspaceAtPoint(
        { scale: scaleRef.current, off: offRef.current },
        clientPointInElement({ x: e.clientX, y: e.clientY }, rect),
        e.deltaY,
      )
      setScale(next.scale)
      setOff(next.off)
    }
    stage.addEventListener('wheel', onWheel, { passive: false })
    return () => stage.removeEventListener('wheel', onWheel)
    // deps 含 src:画布(.ze-mask__stage)在 src 异步到位后才挂载(初始无选中实体时渲 CanvasViewport
    // 而非 MaskCanvas,stageRef 为空)。空 deps 只在首渲挂一次 → stage 后到就永不补挂、滚轮缩放失效。
    // 依赖 src 后,stage 挂载那次重渲会重跑本 effect、补挂 wheel 监听。
  }, [src])

  const scheduleCommit = useCallback(() => {
    if (!onCommitRef.current) return
    if (commitTimer.current) clearTimeout(commitTimer.current)
    commitTimer.current = setTimeout(() => {
      const m = maskRef.current
      const commit = onCommitRef.current
      if (!m || !commit) return
      try {
        const tmp = document.createElement('canvas')
        tmp.width = m.w; tmp.height = m.h
        const ctx = tmp.getContext('2d')
        if (!ctx) return
        const img = ctx.createImageData(m.w, m.h)
        img.data.set(alphaToRGBA(m, tintRef.current))
        ctx.putImageData(img, 0, 0)
        tmp.toBlob((blob) => {
          if (!blob) return
          blob.arrayBuffer().then((buf) => commit(new Uint8Array(buf)))
        }, 'image/png')
      } catch { /* 编码失败静默 */ }
    }, commitDebounceMs)
  }, [commitDebounceMs])

  const toImagePoint = useCallback((e: React.PointerEvent): MaskPoint | null => {
    const stage = stageRef.current
    const m = maskRef.current
    if (!stage || !m) return null
    const rect = stage.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return null
    return clampPointToSize(
      screenToWorld(clientPointInElement({ x: e.clientX, y: e.clientY }, rect), { scale: scaleRef.current, off: offRef.current }),
      { w: m.w, h: m.h },
    )
  }, [])

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    if (disabled) return
    if (e.button === 1) {
      e.preventDefault()
      e.currentTarget.setPointerCapture(e.pointerId)
      panning.current = { x: e.clientX, y: e.clientY, ox: offRef.current.x, oy: offRef.current.y }
      return
    }
    if (e.shiftKey && e.button === 2) {
      e.preventDefault()
      e.currentTarget.setPointerCapture(e.pointerId)
      brushAdjust.current = { x: e.clientX, y: e.clientY, r: radius, h: hardness }
      return
    }
    if (e.button !== 0) return
    const p = toImagePoint(e)
    const m = maskRef.current
    if (!p || !m) return
    pushHistory()
    e.currentTarget.setPointerCapture(e.pointerId)
    drawing.current = { last: p }
    setCursor(p)
    setMask(applyStroke(m, [p], { radius, mode, hardness, opacity: brushOpacity, flow: brushFlow, spacing: brushSpacing }))
  }, [disabled, toImagePoint, pushHistory, radius, hardness, brushOpacity, brushFlow, brushSpacing, mode])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (panning.current) {
      const d = panning.current
      setOff({ x: d.ox + e.clientX - d.x, y: d.oy + e.clientY - d.y })
      return
    }
    if (brushAdjust.current) {
      const d = brushAdjust.current
      setRadius(Math.max(1, Math.min(512, Math.round(d.r + (e.clientX - d.x) * 0.5))))
      setHardness(Math.max(0, Math.min(100, Math.round(d.h - (e.clientY - d.y) * 0.5))))
      return
    }
    const hover = toImagePoint(e)
    setCursor(hover)
    if (!drawing.current) return
    const p = hover
    const m = maskRef.current
    if (!p || !m) return
    setMask(applyStroke(m, [drawing.current.last, p], { radius, mode, hardness, opacity: brushOpacity, flow: brushFlow, spacing: brushSpacing }))
    drawing.current = { last: p }
  }, [toImagePoint, radius, hardness, brushOpacity, brushFlow, brushSpacing, mode])

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    if (panning.current || brushAdjust.current) {
      panning.current = null
      brushAdjust.current = null
      try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
      return
    }
    if (!drawing.current) return
    drawing.current = null
    try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
    scheduleCommit()
  }, [scheduleCommit])

  const onPointerLeave = useCallback(() => {
    if (!drawing.current && !panning.current && !brushAdjust.current) setCursor(null)
  }, [])

  const clear = useCallback(() => {
    const m = maskRef.current
    if (!m) return
    pushHistory()
    setMask(createMask(m.w, m.h))
    scheduleCommit()
  }, [pushHistory, scheduleCommit])

  const undo = useCallback(() => {
    const prev = history.current.pop()
    const cur = maskRef.current
    if (!prev || !cur) return
    redo.current.push(cloneMask(cur))
    setMask(prev)
    syncHistoryFlags()
    scheduleCommit()
  }, [syncHistoryFlags, scheduleCommit])

  const redoFn = useCallback(() => {
    const next = redo.current.pop()
    const cur = maskRef.current
    if (!next || !cur) return
    history.current.push(cloneMask(cur))
    setMask(next)
    syncHistoryFlags()
    scheduleCommit()
  }, [syncHistoryFlags, scheduleCommit])

  return {
    controls: {
      mode, setMode, radius, setRadius, hardness, setHardness,
      undo, redo: redoFn, clear, canUndo, canRedo, disabled,
    },
    canvas: {
      src, mask, mode, radius, hardness, scale, off, cursor,
      stageRef, canvasRef, onPointerDown, onPointerMove, onPointerUp, onPointerLeave, fitToView,
    },
  }
}
