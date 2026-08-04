/**
 * Slider — 数值滑杆 primitive(框架级)
 *
 * 原生 <input type=range>(皮肤 .ze-slider 已自定义 track/thumb)+ 右侧数值读数。
 * FormEditor 在 schema 声明 minimum/maximum 时用它替纯数字输入;ParamPanel 也可复用。
 * 受控:value 进,onChange(拖动实时)/ onCommit(松手 change 事件)出。
 * 视觉纯组合 L2 原子:行 = .ze-row;读数 = .ze-text[role=tertiary] + .ze-field-row__value
 * (固定宽右槽,控件列几何法);滑杆 = .ze-slider。
 */

import type { ReactNode } from 'react'

export type SliderProps = {
  value: number
  min: number
  max: number
  step?: number
  disabled?: boolean
  onChange?: (v: number) => void
  onCommit?: (v: number) => void
}

function fmt(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

export function Slider({ value, min, max, step, disabled, onChange, onCommit }: SliderProps): ReactNode {
  const safe = Number.isFinite(value) ? value : min
  return (
    <div className="ze-row" data-gap="2">
      <input
        type="range"
        className="ze-slider"
        min={min}
        max={max}
        step={step ?? 'any'}
        value={safe}
        disabled={disabled}
        onChange={(e) => onChange?.(Number(e.target.value))}
        onMouseUp={(e) => onCommit?.(Number((e.target as HTMLInputElement).value))}
        onKeyUp={(e) => onCommit?.(Number((e.target as HTMLInputElement).value))}
      />
      <span className="ze-text ze-field-row__value" data-role="tertiary">{fmt(safe)}</span>
    </div>
  )
}
