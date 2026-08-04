/**
 * MaskToolbarZone — 历史命名的像素层绘制工具功能区(控制类·操作区;03-zones/SPEC §toolbar)
 *
 * 可绘制像素层的**唯一**工具功能区:画笔/橡皮(切 mode)+ 笔刷大小/硬度(滑块)+ 撤销/重做/清除
 * +(可选)mod 运行按钮(Complete object 等)。这张像素图是否被当成 mask/range/alpha,由 mod 决定。
 * 用 v2 `.ze-editor__zone`(与其他操作行统一规则),状态来自 useMaskEditor hook 的 controls。
 *
 * 取代旧的「MaskCanvas 自带 .ze-mask__toolbar + canvas2d 顶 ToolRowZone(tools)」两套重复工具栏。
 */

import { type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { Slider } from '../primitives/Slider'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import type { MaskControls } from '../primitives/useMaskEditor'
import type { DispatchFn } from '../types'

/** mod 运行按钮(运行态置灰;command 走 dispatch,声明式无裸 onClick)。
 *  文案=命令动词=可译 chrome:labelKey(走 t())优先,回退 label(语言中立直显,兼容旧声明)。 */
export interface MaskRunAction {
  command: string
  labelKey?: string
  label?: string
  args?: Record<string, unknown>
  disabled?: boolean
  variant?: 'primary' | 'ghost' | 'danger'
}

export interface MaskToolbarZoneProps {
  controls: MaskControls
  runAction?: MaskRunAction
  dispatch?: DispatchFn
}

export function MaskToolbarZone({ controls, runAction, dispatch }: MaskToolbarZoneProps): ReactNode {
  const t = useT()
  const hugRef = useScrollAffordance('x')
  const actionsRef = useScrollAffordance('x')
  // 撤销/重做不在工具区出按钮 —— 它是工作台级全局(footer + ⌘Z;SPEC §222)。画布内逐笔
  // 撤销走键盘 ⌘Z/⌘⇧Z(Canvas2dMaskEditor 经 useEditorShortcuts 接,画布聚焦作用域,绘图惯例)。
  const {
    mode, setMode, radius, setRadius, hardness, setHardness,
    clear, disabled,
  } = controls
  // 工具区完整逻辑:工具(画笔/橡皮)→ 参数(大小/硬度)→ 编辑(清除)→ 运行(右对齐),
  // 组间用细竖线 .ze-editor__zone-sep 分隔,语义分组一目了然。
  return (
    <div
      ref={hugRef}
      className="ze-editor__zone"
      data-zone="toolbar" data-role="toolbar" data-layout="hug-scroll"
    >
      {/* 工具组:画笔 / 橡皮(互斥模式切换) */}
      <button
        type="button" className="ze-button" data-size="sm"
        data-variant={mode === 'brush' ? 'solid' : 'ghost'}
        aria-pressed={mode === 'brush'} disabled={disabled}
        onClick={() => setMode('brush')}
      >{t('tool.brush')}</button>
      <button
        type="button" className="ze-button" data-size="sm"
        data-variant={mode === 'eraser' ? 'solid' : 'ghost'}
        aria-pressed={mode === 'eraser'} disabled={disabled}
        onClick={() => setMode('eraser')}
      >{t('tool.eraser')}</button>
      <span className="ze-editor__zone-sep" aria-hidden="true" />
      {/* 参数条:大小 / 硬度(跟随当前工具) */}
      <span className="ze-mask__brush">
        <span className="ze-mask__brush-label">{t('editor.maskBrushSize')}</span>
        <Slider value={radius} min={1} max={120} step={1} disabled={disabled} onChange={setRadius} />
      </span>
      <span className="ze-mask__brush">
        <span className="ze-mask__brush-label">{t('editor.maskHardness')}</span>
        <Slider value={hardness} min={0} max={100} step={1} disabled={disabled} onChange={setHardness} />
      </span>
      <span className="ze-editor__zone-sep" aria-hidden="true" />
      {/* 编辑动作:清除(产出一条空遮罩 Operation,可被全局撤销) */}
      <button
        type="button" className="ze-button" data-size="sm" data-variant="ghost"
        disabled={disabled} onClick={clear}
      >{t('editor.maskClear')}</button>
      {/* 运行动作:右对齐(mod 注入,如 Complete object) */}
      {runAction && (
        <div ref={actionsRef} className="ze-editor__zone-actions ze-scroll-fade-x">
          <button
            type="button" className="ze-button" data-size="sm"
            data-variant={runAction.variant ?? 'primary'} disabled={runAction.disabled}
            onClick={() => dispatch?.(runAction.command, runAction.args ?? {})}
          >{runAction.labelKey ? t(runAction.labelKey) : (runAction.label ?? '')}</button>
        </div>
      )}
    </div>
  )
}
