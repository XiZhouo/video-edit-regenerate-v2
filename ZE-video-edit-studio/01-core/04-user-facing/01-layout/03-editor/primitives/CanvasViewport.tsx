/**
 * CanvasViewport — 2D 视口 primitive(拖入导入 + 缩放/平移 + 叠加层)
 *
 * canvas2d / asset editor 复用。模板默认形态 = 纯视口(拖入 + 滚轮缩放 + 拖拽平移),
 * 不带任何绘制工具栏(SAM/画笔是 opt-in primitive,见 CanvasToolbar)。
 * 受控显示一张图(src 经 assetUrl 解析),children 作叠加层(bbox / 描边等)。
 * 视觉:03-css-rules/02-atoms/spatial.css 的 .ze-canvas*(L2 组件原子)。
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { clientPointInElement, fitWorkspace, zoomWorkspaceAtPoint } from './canvasWorkspace'

export type CanvasViewportProps = {
  src?: string
  onDropFile?: (file: File) => void
  children?: ReactNode
  emptyText?: string
}

export function CanvasViewport({
  src, onDropFile, children, emptyText,
}: CanvasViewportProps): ReactNode {
  const t = useT()
  const [scale, setScale] = useState(1)
  const [off, setOff] = useState({ x: 0, y: 0 })
  const [over, setOver] = useState(false)
  const [imageSize, setImageSize] = useState<{ w: number; h: number } | null>(null)
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null)
  // off 镜像进 ref:onMouseDown 读 ref 拿最新原点,避免 SSE 重渲染中途拖拽时闭包过期跳变
  const offRef = useRef(off)
  offRef.current = off
  const scaleRef = useRef(scale)
  scaleRef.current = scale
  const rootRef = useRef<HTMLDivElement>(null)

  const fit = useCallback((size = imageSize) => {
    const el = rootRef.current
    if (!el || !size || size.w <= 0 || size.h <= 0) { setScale(1); setOff({ x: 0, y: 0 }); return }
    const rect = el.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return
    const next = fitWorkspace({ w: rect.width, h: rect.height }, size)
    setScale(next.scale)
    setOff(next.off)
  }, [imageSize])

  // 滚轮缩放:用非被动监听(passive:false)→ preventDefault 真生效、阻止页面滚动。
  // (React 的 onWheel 是被动监听,preventDefault 无效且报 "Unable to preventDefault" 警告。)
  useEffect(() => {
    const el = rootRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const next = zoomWorkspaceAtPoint(
        { scale: scaleRef.current, off: offRef.current },
        clientPointInElement({ x: e.clientX, y: e.clientY }, rect),
        e.deltaY,
      )
      setScale(next.scale)
      setOff(next.off)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button === 1) e.preventDefault()
    drag.current = { x: e.clientX, y: e.clientY, ox: offRef.current.x, oy: offRef.current.y }
  }, [])

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    const d = drag.current
    if (!d) return
    setOff({ x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) })
  }, [])

  const endDrag = useCallback(() => { drag.current = null }, [])

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setOver(false)
    const f = e.dataTransfer.files?.[0]
    if (f && onDropFile) onDropFile(f)
  }, [onDropFile])

  const onPaste = useCallback((e: React.ClipboardEvent) => {
    if (!onDropFile) return
    const f = Array.from(e.clipboardData.files ?? []).find((x) => x.type.startsWith('image/'))
    if (!f) return
    e.preventDefault()
    onDropFile(f)
  }, [onDropFile])

  return (
    <div
      className="ze-canvas"
      ref={rootRef}
      data-dragover={over ? 'true' : 'false'}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={endDrag}
      onMouseLeave={endDrag}
      onDragOver={onDropFile ? (e) => { e.preventDefault(); setOver(true) } : undefined}
      onDragLeave={onDropFile ? () => setOver(false) : undefined}
      onDrop={onDropFile ? onDrop : undefined}
      onPaste={onDropFile ? onPaste : undefined}
      onDoubleClick={() => fit()}
      tabIndex={onDropFile ? 0 : undefined}
    >
      {src ? (
        <div className="ze-canvas__stage" style={{ transform: `translate(${off.x}px, ${off.y}px) scale(${scale})` }}>
          <img
            className="ze-canvas__img"
            src={src}
            alt="canvas"
            draggable={false}
            onLoad={(e) => {
              const img = e.currentTarget
              const size = { w: img.naturalWidth || img.width, h: img.naturalHeight || img.height }
              setImageSize(size)
              window.requestAnimationFrame(() => fit(size))
            }}
          />
          {children}
        </div>
      ) : (
        <div className="ze-canvas__empty">{emptyText ?? t('editor.dropImage')}</div>
      )}
      {src && (
        <div className="ze-canvas__hud">
          <span>{Math.round(scale * 100)}%</span>
          <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => fit()}>{t('editor.reset')}</button>
        </div>
      )}
    </div>
  )
}
