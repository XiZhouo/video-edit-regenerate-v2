/**
 * ControlZone — ToolZone 兼容实现(控制类·工具/命令/过程生命周期;03-zones/SPEC §tool)
 *
 * 与 ManageZone(搜索/筛选/排序/选择管理)分工:ToolZone 收**对象/过程生命周期**命令
 * (暂停/继续/中断/取消/重试/导出 run notebook)。渲染 .ze-editor__zone[data-function-zone=tool][data-role=control]
 * [data-lines=1][data-layout=hug-scroll](复用 __zone 行几何,几何令牌住 L2 editor-shell.css)。
 * 命令全走 ToolbarAction[kind=command] → renderToolbarAction → dispatch(禁裸 onClick;声明式)。
 * actions 为空则整行不渲(无生命周期命令时不占行)。
 */

import type { ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { renderToolbarAction } from './renderToolbarAction'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import type { ToolbarAction } from './types'
import type { DispatchFn } from '../types'

export interface ControlZoneProps {
  /** 生命周期命令件(受 ToolbarAction[kind=command] 约束:command + args + disabled + variant)。 */
  actions: ToolbarAction[]
  dispatch?: DispatchFn
}

export function ControlZone({ actions, dispatch }: ControlZoneProps): ReactNode {
  const t = useT()
  const hugRef = useScrollAffordance('x')
  if (actions.length === 0) return null
  return (
    <div
      ref={hugRef}
      className="ze-editor__zone"
      data-zone="tool" data-role="control" data-function-zone="tool" data-lines="1" data-layout="hug-scroll"
    >
      {actions.map((a) => renderToolbarAction(a, t, dispatch, undefined, undefined))}
    </div>
  )
}
