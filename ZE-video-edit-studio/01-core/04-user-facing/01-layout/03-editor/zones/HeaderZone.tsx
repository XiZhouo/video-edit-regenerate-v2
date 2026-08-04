/**
 * HeaderZone — 身份行(信息类;03-zones/SPEC §header)
 *
 * 固定骨架:title + count/meta(只读)。**条件功能区**:多面板区分 / 有动态信息(计数·任务态) /
 * 编辑器级动作 才传(由编辑器/布局决定是否传 header 槽,见 SPEC「header 条件机制」)。
 * 禁输入控件(搜索/cycle/筛选下沉 toolbar/filterbar) —— 门禁 ZONE_HEADER_PURE。
 * 复用现有 .ze-editor__title / .ze-editor__count;容器 .ze-editor__header(几何同现 __toolbar)。
 */

import type { ReactNode } from 'react'

export interface HeaderZoneProps {
  /** 数据显示名(typeLabel / 实体名,语言中立,直显)。 */
  title?: string
  /** 计数 / 元信息槽:仅只读显示(数字 / 文本 / 进度环·状态徽章);非交互。 */
  count?: ReactNode
}

export function HeaderZone({ title, count }: HeaderZoneProps): ReactNode {
  return (
    <div className="ze-editor__header" data-zone="header">
      {title != null && <span className="ze-editor__title">{title}</span>}
      {count != null && <span className="ze-editor__count">{count}</span>}
    </div>
  )
}
