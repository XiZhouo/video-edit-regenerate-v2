/**
 * FunctionZone — 编辑器功能区骨架的单一出口。
 *
 * 业务 editor 可以决定本行放哪些控件,但不能再手写 `.ze-editor__zone` 外壳。
 * 边界语义由 core 统一映射到 data-*:
 * - tool/control/toolbar: 默认 control-group,无四边块。
 * - manage/filterbar: 搜索/筛选/选择管理,按布局换行或横滚。
 * - boundary: 只有确需 panel/viewport/separator 时显式声明。
 */

import type { ReactNode } from 'react'
import { useScrollAffordance } from '../lib/useScrollAffordance'

export type FunctionZoneRole = 'toolbar' | 'filterbar' | 'control' | 'transport' | 'actionbar'
export type FunctionZoneKind = 'tool' | 'manage'
export type FunctionZoneLayout = 'hug-scroll' | 'wrap-cluster' | 'flex-fill'
export type FunctionZoneBoundary = 'panel' | 'viewport' | 'separator'

export interface FunctionZoneProps {
  id: string
  role: FunctionZoneRole
  kind?: FunctionZoneKind
  layout?: FunctionZoneLayout
  lines?: 1
  boundary?: FunctionZoneBoundary
  className?: string
  children: ReactNode
  /** 仅给 button groups / filter groups 这类需要 ARIA group 语义的行使用。 */
  ariaRole?: 'group'
}

export function FunctionZone({
  id,
  role,
  kind,
  layout = 'hug-scroll',
  lines = 1,
  boundary,
  className,
  children,
  ariaRole,
}: FunctionZoneProps): ReactNode {
  const classes = className ? `ze-editor__zone ${className}` : 'ze-editor__zone'
  // hug-scroll 行横向溢出时才淡出(状态化);其余布局(wrap-cluster/flex-fill)不横滚,不接。
  const hugRef = useScrollAffordance('x')
  // data-lines=1 锁单行高 + overflow:hidden,只对单行横滚布局(hug-scroll)成立。
  // flex-fill / wrap-cluster 是**按设计换行**的:套 data-lines=1 会把换行的第二行整排裁掉
  // (实测:gallery 控制条选中后按钮多到换行,导出 ZIP 落第二行被裁、找不到入口)。
  // 故换行布局不发 data-lines,让区高随内容长。
  const lineLock = layout === 'hug-scroll' ? lines : undefined
  return (
    <div
      ref={layout === 'hug-scroll' ? hugRef : undefined}
      className={classes}
      data-zone={id}
      data-role={role}
      data-function-zone={kind}
      data-lines={lineLock}
      data-layout={layout}
      data-boundary={boundary}
      role={ariaRole}
    >
      {children}
    </div>
  )
}
