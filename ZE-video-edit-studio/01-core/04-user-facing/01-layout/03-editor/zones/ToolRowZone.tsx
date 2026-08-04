/**
 * ToolRowZone — 工具行(控制类·模式;03-zones/SPEC §toolbar)
 *
 * 固定骨架:画布工具(CanvasToolbar:模式互斥单选)+ 可选命令件(导出 i18n)
 * + 可选运行按钮(mod 业务命令)。渲染 .ze-editor__zone[data-role=toolbar][data-lines=1]
 * [data-layout=hug-scroll](单行 1×row-h,超宽横向隐滚)。**工具条是一个功能区**:工具(模式)+
 * 命令件分组是内部结构,不拆多 zone。命令全走 dispatch(禁裸 onClick)。
 * 撤销/重做不在工具行 —— 全局(footer + ⌘Z;SPEC §222)。
 *
 * commands 拓展点支持 ToolbarAction 全四 kind(command/toggle/search/cycle):toggle/search/cycle
 * 类需读写视图态,经可选 viewState/onViewChange 透传给 renderToolbarAction(gallery 工具条的
 * 搜索框/对比 toggle 走此路;cardlist 尺寸不走运行时 cycle)。canvas2d 现调用不传 → 退回 command-only,不受影响。
 */

import type { ReactNode } from 'react'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { CanvasToolbar } from '../primitives/CanvasToolbar'
import type { ToolId } from '../primitives/canvasTools'
import { renderToolbarAction } from './renderToolbarAction'
import type { ToolbarAction } from './types'
import type { DispatchFn } from '../types'

/** mod 运行按钮(数据 label·运行态 disabled;command 走 dispatch,声明式无裸 onClick)。 */
export interface ToolRowRunAction {
  command: string
  /** 运行钮文案=命令动词=可译 chrome:labelKey(走 t())优先,回退 label(语言中立直显,兼容旧声明)。 */
  labelKey?: string
  label?: string
  args?: Record<string, unknown>
  disabled?: boolean
  variant?: 'primary' | 'ghost' | 'danger'
}

export interface ToolRowZoneProps {
  /** 画布工具子集(闭集枚举;互斥单选);不传则无工具组。 */
  tools?: ToolId[]
  active?: ToolId
  onChange?: (t: ToolId) => void
  /** 工具行内命令/视图态件(导出等框架文案 + 搜索/档位/开关;受 ToolbarAction 四 kind 约束)。 */
  commands?: ToolbarAction[]
  /** mod 运行按钮(数据 label·运行态置灰)。 */
  runAction?: ToolRowRunAction
  dispatch?: DispatchFn
  /** toggle/search/cycle 类 commands 的视图态读写(编辑器持有,frame 透传);command 类不需要。 */
  viewState?: Record<string, unknown>
  onViewChange?: (key: string, val: unknown) => void
}

export function ToolRowZone({
  tools, active, onChange, commands, runAction, dispatch,
  viewState, onViewChange,
}: ToolRowZoneProps): ReactNode {
  const t = useT()
  const hugRef = useScrollAffordance('x')
  const actionsRef = useScrollAffordance('x')
  const hasActions = (commands && commands.length > 0) || runAction != null
  return (
    <div
      ref={hugRef}
      className="ze-editor__zone"
      data-zone="toolbar" data-role="toolbar" data-lines="1" data-layout="hug-scroll"
    >
      {tools && tools.length > 0 && onChange && (
        <CanvasToolbar
          tools={tools} active={active ?? tools[0]} onChange={onChange}
        />
      )}
      {hasActions && (
        <div ref={actionsRef} className="ze-editor__zone-actions ze-scroll-fade-x">
          {commands?.map((a) => renderToolbarAction(a, t, dispatch, viewState, onViewChange))}
          {runAction && (
            <button
              type="button" className="ze-button" data-size="sm"
              data-variant={runAction.variant ?? 'primary'} disabled={runAction.disabled}
              onClick={() => dispatch?.(runAction.command, runAction.args ?? {})}
            >{runAction.labelKey ? t(runAction.labelKey) : (runAction.label ?? '')}</button>
          )}
        </div>
      )}
    </div>
  )
}
