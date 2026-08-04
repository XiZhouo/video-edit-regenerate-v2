/**
 * CanvasToolbarZone — 画布工具条(操作区 zone;SPEC §2A `canvas-toolbar`)
 *
 * 固定骨架:`.ze-editor__toolbar` 槽 = 标题 + 可选画布工具(CanvasToolbar primitive)。
 * canvas 家族(canvas2d/graph/scene3d)共用的顶栏。拓展点:`tools: ToolId[]`(闭集枚举);
 * 不传 tools 即纯标题栏(纯视口模式)。撤销/重做不在此 —— 全局(footer + ⌘Z;SPEC §222)。
 */

import type { ReactNode } from 'react'
import { CanvasToolbar } from '../primitives/CanvasToolbar'
import type { ToolId } from '../primitives/canvasTools'

export type CanvasToolbarZoneProps = {
  title: string
  /** 拓展点:画布工具子集(闭集枚举);不传则纯标题栏。 */
  tools?: ToolId[]
  active?: ToolId
  onChange?: (t: ToolId) => void
}

export function CanvasToolbarZone({
  title, tools, active, onChange,
}: CanvasToolbarZoneProps): ReactNode {
  return (
    <div className="ze-editor__toolbar" data-zone="canvas-toolbar">
      <span className="ze-editor__title">{title}</span>
      {tools && tools.length > 0 && onChange && (
        <div className="ze-editor__toolbar-actions">
          <CanvasToolbar
            tools={tools} active={active ?? tools[0]} onChange={onChange}
          />
        </div>
      )}
    </div>
  )
}
