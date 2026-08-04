/**
 * Canvas2dLayerStage — layered 合成/绘制舞台 primitive for `canvas2d`(纯画布职责)。
 *
 * 1.27.0 由 Canvas2dLayerEditor 纯化更名:图层**列表面板与属性小面板已摘除**——
 * 图层列表=gallery flat list 或 outline tree/group 实例、属性=form 编辑器,经 L5 layered_canvas
 * 组合;多 editor 零直连,全经全局 selection[<layer 桶>](SPEC §2.1b 三律)。
 *
 * 本舞台保留画布自身职责:视口(fit/zoom/pan,canvasWorkspace)、按 order 合成渲染、
 * PS-lite 笔刷绘制、工具建层(text/rect/ellipse/mask/bbox/导图/粘贴)、裁切、导出。
 * 数据源=layer 实体(适配层映射好的 Canvas2dLayer[]);活动层=props.activeLayerId(全局选择注入),
 * 点击层→onSelectLayer(单向派发,不持有选择态);**绘制要求显式选中**(空选只读,防误画)。
 * 持久化全走回调→命令通道:onLayerPatch(entity.update CAS)/onLayerAdd/onLayerPaint/onCrop。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode, type WheelEvent } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict } from '../types'
import type { ToolId } from './canvasTools'
import { Slider } from './Slider'
import {
  canvas2dLayerFilterCss,
  canvas2dLayerCanEdit,
  canvas2dLayerStyle,
  createCanvas2dLayer,
  type Canvas2dFeatureFlags,
  type Canvas2dLayer,
} from './canvas2dModel'
import { clampNumber, clientPointInElement, fitWorkspace, screenToWorld, zoomWorkspaceAtPoint } from './canvasWorkspace'
import { appendFreehandPoint, isClosableRing, isNearPoint } from '../lib/canvas2dSelection'

type UploadAssetFn = (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>

export type Canvas2dLayerStageProps = {
  /** 锚/宿主实体(stage 尺寸 width/height + 背景 + 导出命名);纯图层组合可为 null(回退层外接+960×640)。 */
  entity: EntityDict | null
  /** 已按 order 升序的层栈(适配层经 readLayerEntities 从 layer 实体映射;Stage 不读 store)。 */
  layers: Canvas2dLayer[]
  /** 活动层 = 全局 selection[<layer 桶>](slot 适配器注入;Stage 不持有选择态)。 */
  activeLayerId: string | null
  onSelectLayer?: (id: string) => void
  /** 层属性写出口(适配层 → entity.update CAS;逐层版本)。 */
  onLayerPatch?: (layerId: string, patch: Partial<Canvas2dLayer>) => void
  /** 建层出口(适配层 → layerAddCommand 或内置 entity.add;order 由适配层补 max+1)。 */
  onLayerAdd?: (layer: Canvas2dLayer) => void
  /** 绘制提交出口(适配层 → paintCommand 或 entity.update assetRef/maskRef)。 */
  onLayerPaint?: (layerId: string, ref: AssetRefDict, kind: Canvas2dLayer['kind']) => void
  /** 裁切提交出口(适配层 → 锚 width/height + 全层平移的多 op 派发)。 */
  onCrop?: (rect: { x: number; y: number; w: number; h: number }) => void
  assetUrl?: AssetUrlFn
  uploadAsset?: UploadAssetFn
  dispatch?: DispatchFn
  bgField?: string
  activeTool: ToolId
  features: Canvas2dFeatureFlags
  exportCommand?: string
  onDropFile?: (file: File) => void
}

type StageSize = { w: number; h: number }
type Point = { x: number; y: number }

function pickRef(data: Record<string, unknown>, field?: string): AssetRefDict | null {
  const asRef = (v: unknown) => (v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? v as AssetRefDict : null)
  if (field) return asRef(data[field])
  for (const v of Object.values(data)) {
    const ref = asRef(v)
    if (ref) return ref
  }
  return null
}

function newLayerId(): string {
  return `layer_${(globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)).replaceAll('-', '').slice(0, 10)}`
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('image load failed'))
    img.src = src
  })
}

function fileSize(file: File): Promise<StageSize> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      resolve({ w: img.naturalWidth || img.width || 256, h: img.naturalHeight || img.height || 256 })
      URL.revokeObjectURL(url)
    }
    img.onerror = () => {
      resolve({ w: 256, h: 256 })
      URL.revokeObjectURL(url)
    }
    img.src = url
  })
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : '.png'
}

function strokeRectWithRadius(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  const radius = Math.max(0, Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2))
  if (!radius) {
    ctx.strokeRect(x, y, w, h)
    return
  }
  ctx.beginPath()
  ctx.moveTo(x + radius, y)
  ctx.lineTo(x + w - radius, y)
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius)
  ctx.lineTo(x + w, y + h - radius)
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h)
  ctx.lineTo(x + radius, y + h)
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius)
  ctx.lineTo(x, y + radius)
  ctx.quadraticCurveTo(x, y, x + radius, y)
  ctx.closePath()
  ctx.stroke()
}

// 画笔像素色 [r,g,b,a]:canvas 2d ctx.fillStyle 需具体色值,无法直接吃 CSS 令牌(否则要 getComputedStyle
// 运行时耦合);故此处具名常量而非散落的 magic number。mask-selection=青石蓝半透选区,raster=墨色实绘。
const MASK_PAINT_RGBA: readonly [number, number, number, number] = [90, 214, 204, 0.72]
const RASTER_PAINT_RGBA: readonly [number, number, number, number] = [24, 24, 24, 0.82]

function paintStamp(ctx: CanvasRenderingContext2D, point: Point, radius: number, hardness: number, erase: boolean, kind: Canvas2dLayer['kind']): void {
  const r = Math.max(1, radius)
  const hard = clampNumber(hardness, 0, 100) / 100
  const inner = clampNumber(r * hard, 0, r)
  ctx.save()
  ctx.globalCompositeOperation = erase ? 'destination-out' : 'source-over'
  const color = kind === 'mask-selection' ? MASK_PAINT_RGBA : RASTER_PAINT_RGBA
  if (hard >= 0.98) {
    ctx.fillStyle = `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${color[3]})`
  } else {
    const gradient = ctx.createRadialGradient(point.x, point.y, inner, point.x, point.y, r)
    gradient.addColorStop(0, `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${color[3]})`)
    gradient.addColorStop(1, `rgba(${color[0]}, ${color[1]}, ${color[2]}, 0)`)
    ctx.fillStyle = gradient
  }
  ctx.beginPath()
  ctx.arc(point.x, point.y, r, 0, Math.PI * 2)
  ctx.fill()
  ctx.restore()
}

export function Canvas2dLayerStage({
  entity, layers, activeLayerId, onSelectLayer, onLayerPatch, onLayerAdd, onLayerPaint, onCrop,
  assetUrl, uploadAsset, dispatch, bgField, activeTool, features, exportCommand, onDropFile,
}: Canvas2dLayerStageProps): ReactNode {
  const t = useT()
  const rootRef = useRef<HTMLDivElement>(null)
  const paintCanvasRef = useRef<HTMLCanvasElement>(null)
  const paintLayerSeedRef = useRef<string>('')
  const [scale, setScale] = useState(1)
  const [off, setOff] = useState({ x: 0, y: 0 })
  const [bgSize, setBgSize] = useState<StageSize | null>(null)
  const [drag, setDrag] = useState<null
    | { kind: 'pan'; x: number; y: number; ox: number; oy: number }
    | { kind: 'move'; id: string; x: number; y: number; lx: number; ly: number }
    | { kind: 'crop'; start: Point }
    | { kind: 'lasso' }
  >(null)
  const [previewPatch, setPreviewPatch] = useState<Partial<Canvas2dLayer> | null>(null)
  const [cropPreview, setCropPreview] = useState<null | { x: number; y: number; w: number; h: number }>(null)
  const [cursor, setCursor] = useState<Point | null>(null)
  const [paintDirty, setPaintDirty] = useState(false)
  const [brushRadius, setBrushRadius] = useState(24)
  const [brushHardness, setBrushHardness] = useState(80)
  const drawing = useRef<Point | null>(null)
  const brushAdjust = useRef<{ x: number; y: number; r: number; h: number } | null>(null)
  // lasso(手绘拖拽)/polygon(多点点击)进行中的顶点(世界坐标);ref 为权威累加,state 仅驱动预览。
  const selPointsRef = useRef<Point[]>([])
  const [selPreview, setSelPreview] = useState<Point[]>([])

  const hostData = entity?.data ?? {}
  const bgRef = pickRef(hostData, bgField)
  const bgSrc = bgRef && assetUrl ? assetUrl(bgRef) : undefined
  const stageSize = useMemo<StageSize>(() => {
    const dataW = typeof hostData.width === 'number' ? hostData.width : 0
    const dataH = typeof hostData.height === 'number' ? hostData.height : 0
    if (dataW > 0 && dataH > 0) return { w: dataW, h: dataH }
    const maxLayer = layers.reduce((acc, layer) => ({
      w: Math.max(acc.w, layer.x + layer.width),
      h: Math.max(acc.h, layer.y + layer.height),
    }), { w: 0, h: 0 })
    return {
      w: Math.max(1, dataW, bgSize?.w ?? 0, maxLayer.w, 960),
      h: Math.max(1, dataH, bgSize?.h ?? 0, maxLayer.h, 640),
    }
  }, [bgSize, hostData.height, hostData.width, layers])

  // 活动层 = 显式全局选择;**不做编辑回退**(空选只读展示,防误画到非预期层)。
  const activeLayer = layers.find((layer) => layer.id === activeLayerId) ?? null
  const editable = canvas2dLayerCanEdit(activeLayer)
  const canPaint = !!(editable && activeLayer && (activeLayer.kind === 'mask-selection' || activeLayer.kind === 'raster'))

  useEffect(() => {
    if (!bgSrc) { setBgSize(null); return }
    let alive = true
    loadImage(bgSrc).then((img) => {
      if (alive) setBgSize({ w: img.naturalWidth || img.width, h: img.naturalHeight || img.height })
    }).catch(() => { if (alive) setBgSize(null) })
    return () => { alive = false }
  }, [bgSrc])

  const fit = useCallback((size: StageSize = stageSize) => {
    const el = rootRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return
    const next = fitWorkspace({ w: rect.width, h: rect.height }, size)
    setScale(next.scale)
    setOff(next.off)
  }, [stageSize])

  useEffect(() => { window.requestAnimationFrame(() => fit()) }, [fit, bgSrc])

  const commitLayerAdd = useCallback((layer: Canvas2dLayer) => {
    onLayerAdd?.(layer)
    onSelectLayer?.(layer.id)
  }, [onLayerAdd, onSelectLayer])

  const toWorld = useCallback((e: PointerEvent): Point | null => {
    const el = rootRef.current
    if (!el) return null
    const rect = el.getBoundingClientRect()
    return screenToWorld(clientPointInElement({ x: e.clientX, y: e.clientY }, rect), { scale, off })
  }, [off, scale])

  const hitLayer = useCallback((point: Point): Canvas2dLayer | null => {
    for (let i = layers.length - 1; i >= 0; i--) {
      const layer = layers[i]
      if (!layer.visible) continue
      if (point.x >= layer.x && point.x <= layer.x + layer.width && point.y >= layer.y && point.y <= layer.y + layer.height) return layer
    }
    return null
  }, [layers])

  const ensurePaintSeed = useCallback(async () => {
    const layer = activeLayer
    const canvas = paintCanvasRef.current
    if (!canvas || !layer) return
    const key = `${layer.id}:${layer.maskRef?.sha256 ?? layer.assetRef?.sha256 ?? 'empty'}:${layer.width}x${layer.height}`
    if (paintLayerSeedRef.current === key) return
    canvas.width = Math.max(1, Math.round(layer.width))
    canvas.height = Math.max(1, Math.round(layer.height))
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    const ref = layer.kind === 'mask-selection' ? layer.maskRef : layer.assetRef
    const url = ref && assetUrl ? assetUrl(ref) : undefined
    if (url) {
      try {
        const img = await loadImage(url)
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
      } catch {
        ctx.clearRect(0, 0, canvas.width, canvas.height)
      }
    }
    paintLayerSeedRef.current = key
    setPaintDirty((v) => !v)
  }, [activeLayer, assetUrl])

  useEffect(() => { ensurePaintSeed() }, [ensurePaintSeed])

  const stamp = useCallback((point: Point, erase: boolean) => {
    const layer = activeLayer
    const canvas = paintCanvasRef.current
    if (!layer || !canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const x = point.x - layer.x
    const y = point.y - layer.y
    paintStamp(ctx, { x, y }, brushRadius, brushHardness, erase, layer.kind)
    setPaintDirty((v) => !v)
  }, [activeLayer, brushHardness, brushRadius])

  const stampStroke = useCallback((from: Point, to: Point, erase: boolean) => {
    const dx = to.x - from.x
    const dy = to.y - from.y
    const distance = Math.hypot(dx, dy)
    const spacing = Math.max(1, brushRadius * 0.35)
    const steps = Math.max(1, Math.ceil(distance / spacing))
    for (let i = 0; i <= steps; i++) {
      const t = i / steps
      stamp({ x: from.x + dx * t, y: from.y + dy * t }, erase)
    }
  }, [brushRadius, stamp])

  const commitPaint = useCallback(async () => {
    const layer = activeLayer
    const canvas = paintCanvasRef.current
    if (!layer || !canvas || !uploadAsset) return
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
    if (!blob) return
    const ref = await uploadAsset(new Uint8Array(await blob.arrayBuffer()), 'image/png', '.png')
    if (onLayerPaint) {
      onLayerPaint(layer.id, ref, layer.kind)
    } else {
      onLayerPatch?.(layer.id, layer.kind === 'mask-selection' ? { maskRef: ref } : { assetRef: ref })
    }
  }, [activeLayer, onLayerPaint, onLayerPatch, uploadAsset])

  const clearSelection = useCallback(() => {
    selPointsRef.current = []
    setSelPreview([])
  }, [])

  // lasso/polygon 闭合:把世界坐标顶点环按层本地坐标填到 paint canvas(浏览器栅格化),再走 commitPaint 落盘。
  const fillSelectionAndCommit = useCallback(async (ring: Point[]) => {
    const layer = activeLayer
    const canvas = paintCanvasRef.current
    clearSelection()
    if (!layer || !canvas || !isClosableRing(ring)) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const [r, g, b, a] = layer.kind === 'mask-selection' ? MASK_PAINT_RGBA : RASTER_PAINT_RGBA
    ctx.save()
    ctx.globalCompositeOperation = 'source-over'
    ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${a})`
    ctx.beginPath()
    ring.forEach((p, i) => {
      const x = p.x - layer.x
      const y = p.y - layer.y
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    })
    ctx.closePath()
    ctx.fill()
    ctx.restore()
    setPaintDirty((v) => !v)
    await commitPaint()
  }, [activeLayer, clearSelection, commitPaint])

  const onPointerDown = useCallback((e: PointerEvent) => {
    // HUD/控件上的按下交给控件自身:pan 分支的 setPointerCapture 会吞掉子按钮的 click
    if ((e.target as HTMLElement).closest('.ze-canvas__hud, .ze-layered-canvas__hud-brush, button, input')) return
    const point = toWorld(e)
    if (!point) return
    setCursor(point)
    if (e.shiftKey && e.button === 2 && canPaint) {
      e.preventDefault()
      brushAdjust.current = { x: e.clientX, y: e.clientY, r: brushRadius, h: brushHardness }
      e.currentTarget.setPointerCapture(e.pointerId)
      return
    }
    if (e.button === 1 || activeTool === 'pan') {
      e.preventDefault()
      setDrag({ kind: 'pan', x: e.clientX, y: e.clientY, ox: off.x, oy: off.y })
      e.currentTarget.setPointerCapture(e.pointerId)
      return
    }
    if (activeTool === 'text' && features.text) {
      commitLayerAdd(createCanvas2dLayer('text', {
        id: newLayerId(), name: t('editor.layerText'), text: t('editor.layerText'),
        x: Math.round(point.x), y: Math.round(point.y), width: 240, height: 48,
      }))
      return
    }
    if ((activeTool === 'rect' || activeTool === 'ellipse') && features.transform) {
      commitLayerAdd(createCanvas2dLayer('vector-shape', {
        id: newLayerId(), name: activeTool === 'rect' ? t('tool.rect') : t('tool.ellipse'),
        x: Math.round(point.x), y: Math.round(point.y), width: 160, height: 96,
        shape: { type: activeTool },
      }))
      return
    }
    if ((activeTool === 'brush' || activeTool === 'eraser') && canPaint) {
      e.currentTarget.setPointerCapture(e.pointerId)
      setDrag({ kind: 'move', id: '__paint__', x: e.clientX, y: e.clientY, lx: point.x, ly: point.y })
      drawing.current = point
      stamp(point, activeTool === 'eraser')
      return
    }
    if (activeTool === 'crop' && features.crop && e.button === 0) {
      setDrag({ kind: 'crop', start: point })
      setCropPreview({ x: point.x, y: point.y, w: 0, h: 0 })
      e.currentTarget.setPointerCapture(e.pointerId)
      return
    }
    // lasso:单次拖拽采点 → 抬手闭合填充(下方 onPointerMove/Up 处理)。
    if (activeTool === 'lasso' && features.maskSelection && canPaint && e.button === 0) {
      e.currentTarget.setPointerCapture(e.pointerId)
      selPointsRef.current = [point]
      setSelPreview([point])
      setDrag({ kind: 'lasso' })
      return
    }
    // polygon:逐次点击落顶点;点回起点(≥3)或双击闭合;Esc 取消。
    if (activeTool === 'polygon' && features.maskSelection && canPaint && e.button === 0) {
      const pts = selPointsRef.current
      if (pts.length >= 3 && isNearPoint(point, pts[0], 12 / scale)) {
        void fillSelectionAndCommit(pts)
      } else {
        selPointsRef.current = [...pts, point]
        setSelPreview(selPointsRef.current)
      }
      return
    }
    const hit = hitLayer(point)
    if (hit) onSelectLayer?.(hit.id)
    if ((activeTool === 'select' || activeTool === 'transform') && hit && canvas2dLayerCanEdit(hit)) {
      setDrag({ kind: 'move', id: hit.id, x: e.clientX, y: e.clientY, lx: hit.x, ly: hit.y })
      e.currentTarget.setPointerCapture(e.pointerId)
    }
  }, [activeTool, brushHardness, brushRadius, canPaint, commitLayerAdd, features.crop, features.maskSelection, features.text, features.transform, fillSelectionAndCommit, hitLayer, off.x, off.y, onSelectLayer, scale, stamp, t, toWorld])

  const onPointerMove = useCallback((e: PointerEvent) => {
    if (brushAdjust.current) {
      const d = brushAdjust.current
      setBrushRadius(clampNumber(Math.round(d.r + (e.clientX - d.x) * 0.5), 1, 512))
      setBrushHardness(clampNumber(Math.round(d.h - (e.clientY - d.y) * 0.5), 0, 100))
      return
    }
    const point = toWorld(e)
    if (point) setCursor(point)
    if (!drag) return
    if (drag.kind === 'pan') {
      setOff({ x: drag.ox + e.clientX - drag.x, y: drag.oy + e.clientY - drag.y })
      return
    }
    if (drag.kind === 'crop') {
      if (!point) return
      const x = Math.max(0, Math.min(drag.start.x, point.x))
      const y = Math.max(0, Math.min(drag.start.y, point.y))
      const w = Math.max(0, Math.abs(point.x - drag.start.x))
      const h = Math.max(0, Math.abs(point.y - drag.start.y))
      setCropPreview({ x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) })
      return
    }
    if (drag.kind === 'lasso') {
      if (point) {
        const next = appendFreehandPoint(selPointsRef.current, point, Math.max(1.5, 2 / scale))
        if (next !== selPointsRef.current) {
          selPointsRef.current = next
          setSelPreview(next)
        }
      }
      return
    }
    if (drag.id === '__paint__') {
      if (point) {
        const from = drawing.current ?? point
        stampStroke(from, point, activeTool === 'eraser')
        drawing.current = point
      }
      return
    }
    setPreviewPatch({ x: Math.round(drag.lx + (e.clientX - drag.x) / scale), y: Math.round(drag.ly + (e.clientY - drag.y) / scale) })
  }, [activeTool, drag, scale, stampStroke, toWorld])

  const onPointerUp = useCallback(async (e: PointerEvent) => {
    if (brushAdjust.current) {
      brushAdjust.current = null
      try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      return
    }
    if (!drag) return
    if (drag.kind === 'crop') {
      if (cropPreview && cropPreview.w >= 8 && cropPreview.h >= 8) onCrop?.(cropPreview)
      setCropPreview(null)
      setDrag(null)
      try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      return
    }
    if (drag.kind === 'lasso') {
      const ring = selPointsRef.current
      setDrag(null)
      try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      await fillSelectionAndCommit(ring)
      return
    }
    if (drag.kind === 'move' && drag.id === '__paint__') {
      drawing.current = null
      setDrag(null)
      try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      await commitPaint()
      return
    }
    if (drag.kind === 'move' && previewPatch) onLayerPatch?.(drag.id, previewPatch)
    setPreviewPatch(null)
    setDrag(null)
    try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* noop */ }
  }, [commitPaint, cropPreview, drag, fillSelectionAndCommit, onCrop, onLayerPatch, previewPatch])

  const onWheel = useCallback((e: WheelEvent) => {
    e.preventDefault()
    const el = rootRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const next = zoomWorkspaceAtPoint(
      { scale, off },
      clientPointInElement({ x: e.clientX, y: e.clientY }, rect),
      e.deltaY,
    )
    setScale(next.scale)
    setOff(next.off)
  }, [off, scale])

  const onStageDoubleClick = useCallback(() => {
    // polygon 进行中(≥3 顶点)→ 双击闭合填充;否则双击=适配视口(原行为)。
    if (activeTool === 'polygon' && selPointsRef.current.length >= 3) {
      void fillSelectionAndCommit(selPointsRef.current)
      return
    }
    fit()
  }, [activeTool, fillSelectionAndCommit, fit])

  // 切走 lasso/polygon → 丢弃未闭合选区(防残留半截路径误填)。
  useEffect(() => {
    if (activeTool !== 'lasso' && activeTool !== 'polygon' && selPointsRef.current.length) {
      selPointsRef.current = []
      setSelPreview([])
    }
  }, [activeTool])

  // Esc 取消进行中的选区(多边形漏点/误开时);仅有进行中路径才挂监听。
  useEffect(() => {
    if (!selPreview.length) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); selPointsRef.current = []; setSelPreview([]) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selPreview.length])

  const addMaskLayer = useCallback(() => {
    commitLayerAdd(createCanvas2dLayer('mask-selection', {
      id: newLayerId(), name: t('editor.layerMask'), x: 0, y: 0, width: stageSize.w, height: stageSize.h,
    }))
  }, [commitLayerAdd, stageSize.h, stageSize.w, t])

  const addBboxLayer = useCallback(() => {
    commitLayerAdd(createCanvas2dLayer('bbox-annotation', {
      id: newLayerId(), name: t('editor.layerBbox'), x: Math.round(stageSize.w * 0.2), y: Math.round(stageSize.h * 0.2),
      width: Math.round(stageSize.w * 0.24), height: Math.round(stageSize.h * 0.18),
    }))
  }, [commitLayerAdd, stageSize.h, stageSize.w, t])

  const importRasterLayer = useCallback(async (file: File) => {
    if (!uploadAsset) { onDropFile?.(file); return }
    const [size, ref] = await Promise.all([
      fileSize(file),
      file.arrayBuffer().then((buf) => uploadAsset(new Uint8Array(buf), file.type || 'image/png', extOf(file.name))),
    ])
    commitLayerAdd(createCanvas2dLayer('raster', {
      id: newLayerId(), name: file.name || t('editor.layerRaster'), assetRef: ref, x: 0, y: 0, width: size.w, height: size.h,
    }))
  }, [commitLayerAdd, onDropFile, t, uploadAsset])

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    const file = e.dataTransfer.files?.[0]
    if (file) void importRasterLayer(file)
  }, [importRasterLayer])

  const onPaste = useCallback((e: React.ClipboardEvent) => {
    const file = Array.from(e.clipboardData.files ?? []).find((item) => item.type.startsWith('image/'))
    if (!file) return
    e.preventDefault()
    void importRasterLayer(file)
  }, [importRasterLayer])

  const exportComposite = useCallback(async (mode: 'download' | 'copy' = 'download') => {
    const canvas = document.createElement('canvas')
    canvas.width = stageSize.w
    canvas.height = stageSize.h
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    if (bgSrc) {
      try {
        const img = await loadImage(bgSrc)
        ctx.drawImage(img, 0, 0, bgSize?.w ?? img.naturalWidth, bgSize?.h ?? img.naturalHeight)
      } catch { /* skip missing background */ }
    }
    for (const layer of layers) {
      if (!layer.visible) continue
      ctx.save()
      ctx.globalAlpha = layer.opacity
      ctx.translate(layer.x + layer.width / 2, layer.y + layer.height / 2)
      ctx.rotate((layer.rotation * Math.PI) / 180)
      ctx.scale(layer.flipX ? -1 : 1, layer.flipY ? -1 : 1)
      const x = -layer.width / 2
      const y = -layer.height / 2
      const ref = layer.kind === 'mask-selection' ? layer.maskRef : layer.assetRef
      const url = ref && assetUrl ? assetUrl(ref) : undefined
      if (url) {
        try {
          const img = await loadImage(url)
          ctx.filter = canvas2dLayerFilterCss(layer) ?? 'none'
          ctx.drawImage(img, x, y, layer.width, layer.height)
          ctx.filter = 'none'
        } catch { /* skip missing layer asset */ }
      } else if (layer.kind === 'text') {
        const style = canvas2dLayerStyle(layer)
        ctx.font = `${Math.max(1, style.fontSize ?? 28)}px sans-serif`
        ctx.fillStyle = style.color ?? style.fill ?? 'rgba(20,20,20,0.9)'
        ctx.fillText(layer.text ?? layer.name, x + 8, y + 34)
      } else if (layer.kind === 'vector-shape') {
        const style = canvas2dLayerStyle(layer)
        ctx.strokeStyle = style.stroke ?? style.color ?? 'rgba(20,20,20,0.9)'
        ctx.lineWidth = style.strokeWidth ?? 2
        if (style.fill) {
          ctx.fillStyle = style.fill
          if (layer.shape?.type === 'ellipse') {
            ctx.beginPath()
            ctx.ellipse(0, 0, layer.width / 2, layer.height / 2, 0, 0, Math.PI * 2)
            ctx.fill()
          } else {
            ctx.fillRect(x, y, layer.width, layer.height)
          }
        }
        if (layer.shape?.type === 'ellipse') {
          ctx.beginPath()
          ctx.ellipse(0, 0, layer.width / 2, layer.height / 2, 0, 0, Math.PI * 2)
          ctx.stroke()
        } else {
          strokeRectWithRadius(ctx, x, y, layer.width, layer.height, style.cornerRadius ?? 0)
        }
      } else if (layer.kind === 'bbox-annotation') {
        const style = canvas2dLayerStyle(layer)
        ctx.setLineDash([8, 5])
        ctx.strokeStyle = style.stroke ?? style.color ?? 'rgba(34, 126, 255, 0.95)'
        ctx.lineWidth = style.strokeWidth ?? 3
        strokeRectWithRadius(ctx, x, y, layer.width, layer.height, style.cornerRadius ?? 0)
      }
      ctx.restore()
    }
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
    if (!blob) return
    if (exportCommand && uploadAsset && entity && dispatch) {
      const ref = await uploadAsset(new Uint8Array(await blob.arrayBuffer()), 'image/png', '.png')
      dispatch(exportCommand, {
        type: entity.type,
        id: entity.id,
        image: ref,
        scope: 'visible-composite',
        width: stageSize.w,
        height: stageSize.h,
        ...(activeLayerId ? { layerId: activeLayerId } : {}),
        expected_version: entity.version,
      })
    }
    if (mode === 'copy' && 'clipboard' in navigator && 'ClipboardItem' in window) {
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
      return
    }
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${entity?.name || entity?.id || 'composite'}.png`
    a.click()
    URL.revokeObjectURL(url)
  }, [assetUrl, bgSize?.h, bgSize?.w, bgSrc, dispatch, entity, exportCommand, layers, stageSize.h, stageSize.w, uploadAsset])

  const displayedLayers = layers.map((layer) => (layer.id === activeLayerId && previewPatch ? { ...layer, ...previewPatch } : layer))

  return (
    <div className="ze-layered-canvas">
      <div className="ze-layered-canvas__workbench">
        <div
          ref={rootRef}
          className="ze-layered-canvas__viewport"
          data-tool={activeTool}
          tabIndex={0}
          onWheel={onWheel}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onContextMenu={(e) => {
            if (canPaint && (activeTool === 'brush' || activeTool === 'eraser')) e.preventDefault()
          }}
          onDoubleClick={onStageDoubleClick}
          onDragOver={(e) => e.preventDefault()}
          onDrop={onDrop}
          onPaste={onPaste}
        >
          <div
            className="ze-layered-canvas__stage"
            style={{ width: stageSize.w, height: stageSize.h, transform: `translate(${off.x}px, ${off.y}px) scale(${scale})` }}
          >
            {bgSrc ? <img className="ze-layered-canvas__bg" src={bgSrc} alt="" draggable={false} /> : null}
            {displayedLayers.map((layer) => {
              const ref = layer.kind === 'mask-selection' ? layer.maskRef : layer.assetRef
              const url = ref && assetUrl ? assetUrl(ref) : undefined
              const isActive = layer.id === activeLayerId
              const visual = canvas2dLayerStyle(layer)
              const layerStyle = {
                left: layer.x,
                top: layer.y,
                width: layer.width,
                height: layer.height,
                '--ze-layer-alpha': layer.opacity,
                '--ze-layer-text-color': visual.color ?? visual.fill,
                '--ze-layer-font-size': visual.fontSize ? `${visual.fontSize}px` : undefined,
                '--ze-layer-stroke': visual.stroke ?? visual.color,
                '--ze-layer-fill': visual.fill,
                '--ze-layer-stroke-width': visual.strokeWidth ? `${visual.strokeWidth}px` : undefined,
                '--ze-layer-radius': visual.cornerRadius ? `${visual.cornerRadius}px` : undefined,
                transform: `rotate(${layer.rotation}deg) scale(${layer.flipX ? -1 : 1}, ${layer.flipY ? -1 : 1})`,
                filter: canvas2dLayerFilterCss(layer),
              } as CSSProperties
              return (
                <div
                  key={layer.id}
                  className="ze-layered-canvas__layer"
                  data-kind={layer.kind}
                  data-active={isActive ? 'true' : 'false'}
                  data-visible={layer.visible ? 'true' : 'false'}
                  style={layerStyle}
                >
                  {url && (!isActive || !canPaint) ? <img src={url} alt="" draggable={false} /> : null}
                  {isActive && canPaint ? (
                    <canvas
                      ref={paintCanvasRef}
                      className="ze-layered-canvas__paint"
                      data-dirty={paintDirty ? 'true' : 'false'}
                    />
                  ) : null}
                  {!url && layer.kind === 'text' ? <span className="ze-layered-canvas__text">{layer.text || layer.name}</span> : null}
                  {!url && layer.kind === 'vector-shape' ? <span className="ze-layered-canvas__shape" data-shape={layer.shape?.type === 'ellipse' ? 'ellipse' : 'rect'} /> : null}
                  {layer.kind === 'bbox-annotation' ? <span className="ze-layered-canvas__bbox" /> : null}
                </div>
              )
            })}
            {cursor && (activeTool === 'brush' || activeTool === 'eraser') ? (
              <span
                className="ze-layered-canvas__cursor"
                style={{
                  left: cursor.x,
                  top: cursor.y,
                  '--ze-brush-size': `${brushRadius * 2}px`,
                  '--ze-brush-hardness': `${brushHardness}%`,
                } as CSSProperties}
              >
                <span />
              </span>
            ) : null}
            {cropPreview ? (
              <span
                className="ze-layered-canvas__crop"
                style={{ left: cropPreview.x, top: cropPreview.y, width: cropPreview.w, height: cropPreview.h }}
              />
            ) : null}
            {selPreview.length ? (
              <svg
                className="ze-layered-canvas__selpath"
                width={stageSize.w}
                height={stageSize.h}
                viewBox={`0 0 ${stageSize.w} ${stageSize.h}`}
              >
                <polyline
                  points={(activeTool === 'polygon' && cursor ? [...selPreview, cursor] : selPreview)
                    .map((p) => `${p.x},${p.y}`)
                    .join(' ')}
                  vectorEffect="non-scaling-stroke"
                />
              </svg>
            ) : null}
          </div>
          <div className="ze-canvas__hud">
            <span>{Math.round(scale * 100)}%</span>
            <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => fit()}>{t('editor.fit')}</button>
            {features.maskSelection && onLayerAdd ? (
              <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={addMaskLayer}>{t('editor.layerMask')}</button>
            ) : null}
            {features.bbox && onLayerAdd ? (
              <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={addBboxLayer}>{t('editor.layerBbox')}</button>
            ) : null}
            {features.export ? (
              <>
                <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => void exportComposite('download')}>{t('editor.exportPng')}</button>
                <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => void exportComposite('copy')}>{t('editor.copyPng')}</button>
              </>
            ) : null}
          </div>
          {canPaint ? (
            <div className="ze-layered-canvas__hud-brush">
              <label>
                <span className="ze-text" data-role="secondary" data-size="xs">{t('editor.maskBrushSize')}</span>
                <Slider value={brushRadius} min={1} max={160} step={1} onChange={setBrushRadius} />
              </label>
              <label>
                <span className="ze-text" data-role="secondary" data-size="xs">{t('editor.maskHardness')}</span>
                <Slider value={brushHardness} min={0} max={100} step={1} onChange={setBrushHardness} />
              </label>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
