/**
 * MaskCanvas — 历史命名的光栅像素层画布(stage;opt-in primitive)
 *
 * 底图上盖半透明像素层 + 笔刷光标 + 缩放/平移 HUD。**纯画布,无工具栏** ——
 * 绘制状态/工具(画笔/橡皮/笔刷/硬度/撤销/重做/清除)全在 useMaskEditor hook,工具栏由编辑器
 * chrome 的 MaskToolbarZone(统一功能区)渲染。本组件只消费 hook 返回的 canvas 部分、画 DOM。
 *
 * 红线:core 只把笔画累积成位图,不解释含义;mod 才决定它是不是 mask/range/alpha。
 * 纯位图逻辑在 maskRaster.ts。
 * 视觉:03-css-rules/02-atoms/spatial.css 的 .ze-mask*(L2 组件原子)。
 */

import { type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import type { MaskCanvasState } from './useMaskEditor'

export type MaskCanvasProps = MaskCanvasState

export function MaskCanvas({
  src, mask, mode, radius, hardness, scale, off, cursor,
  stageRef, canvasRef, onPointerDown, onPointerMove, onPointerUp, onPointerLeave, fitToView,
}: MaskCanvasProps): ReactNode {
  const t = useT()
  return (
    <div className="ze-mask">
      <div
        ref={stageRef}
        className="ze-mask__stage"
        onContextMenu={(e) => e.preventDefault()}
        onDoubleClick={() => fitToView()}
      >
        {src ? (
          <div
            className="ze-mask__frame"
            style={{
              width: mask?.w ?? undefined,
              height: mask?.h ?? undefined,
              transform: `translate(${off.x}px, ${off.y}px) scale(${scale})`,
            }}
          >
            <img className="ze-mask__bg" src={src} alt="" draggable={false}
                 width={mask?.w} height={mask?.h} />
            <canvas
              ref={canvasRef}
              className="ze-mask__overlay"
              data-mode={mode}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp}
              onPointerLeave={onPointerLeave}
            />
            {cursor && (
              <span
                className="ze-mask__cursor"
                style={{ left: cursor.x, top: cursor.y, width: radius * 2, height: radius * 2 }}
              >
                <span style={{ width: `${hardness}%`, height: `${hardness}%` }} />
              </span>
            )}
          </div>
        ) : (
          <div className="ze-mask__empty">{t('editor.dropImage')}</div>
        )}
        {src && (
          <div className="ze-canvas__hud">
            <span>{Math.round(scale * 100)}%</span>
            <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => fitToView()}>{t('editor.reset')}</button>
          </div>
        )}
      </div>
    </div>
  )
}
