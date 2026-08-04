/**
 * CanvasToolbar — 画布工具栏(opt-in primitive)
 *
 * 只渲染传入的 tools 子集(按需,不堆全 10 个)。**撤销/重做不在此** —— 它是工作台级全局
 * (footer + ⌘Z;SPEC §222:编辑器工具条禁出 undo/redo 按钮);画布逐笔撤销走画布键盘 ⌘Z。
 * 响应式:size='icon'(窄)只显图标,'icon+label'(宽)带文字。
 * 视觉:纯组合 L2 原子(绝对严格分层)—— 条 = .ze-bar[data-surface=raised][data-edge=top];
 *   工具 = .ze-button[data-variant=ghost](data-active toggle)。无专属 zone class、无自造视觉。
 */

import type { ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { IconOrGlyph } from '../../lib/zeIcon'
import { CANVAS_TOOLS, type ToolId } from './canvasTools'

export type CanvasToolbarProps = {
  tools: ToolId[]
  active: ToolId
  onChange: (t: ToolId) => void
  size?: 'icon' | 'icon+label'
}

export function CanvasToolbar({
  tools, active, onChange, size = 'icon',
}: CanvasToolbarProps): ReactNode {
  const tr = useT()
  return (
    <div className="ze-bar" data-surface="raised" data-edge="top" data-gap="0-5" data-size={size} role="toolbar">
      {tools.map((id) => {
        const def = CANVAS_TOOLS[id]
        if (!def) return null
        const label = tr(def.labelKey)
        return (
          <button
            key={id}
            type="button"
            className="ze-button"
            data-variant="ghost"
            data-size="sm"
            data-active={id === active ? 'true' : 'false'}
            title={label}
            aria-pressed={id === active}
            onClick={() => onChange(id)}
          >
            <IconOrGlyph value={def.icon} />
            {size === 'icon+label' && <span>{label}</span>}
          </button>
        )
      })}
    </div>
  )
}
