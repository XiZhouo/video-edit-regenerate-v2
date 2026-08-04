/**
 * ToolbarZone — 操作条(操作区 zone)
 *
 * 固定骨架:`.ze-editor__toolbar` 的 title + count + 右侧 actions 容器(复用现 editor-shell.css 槽)。
 * 拓展点:actions(声明式 ToolbarAction[]),只接受 command / toggle / search / cycle 受约束语义,
 * 不接受裸 onClick/JSX。command 派发到 dispatch;toggle/search/cycle 读写 viewState。
 * 操作件渲染复用 ./renderToolbarAction(与 ToolRowZone/ControlZone 共享)。
 *
 * 注:v2 起身份(title/count)归 HeaderZone、工具归 ToolRowZone;本组件为迁移期保活的通用操作条,
 * 未迁编辑器继续用(走 EditorFrame 的 @deprecated toolbar 槽)。
 */

import type { ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { renderToolbarAction } from './renderToolbarAction'
import type { ToolbarZoneProps } from './types'

export function ToolbarZone({
  title, count, actions, dispatch, viewState, onViewChange,
}: ToolbarZoneProps): ReactNode {
  const t = useT()
  return (
    <div className="ze-editor__toolbar" data-zone="toolbar">
      {title != null && <span className="ze-editor__title">{title}</span>}
      {count != null && <span className="ze-editor__count">{count}</span>}
      {actions && actions.length > 0 && (
        <div className="ze-editor__toolbar-actions">
          {actions.map((a) => renderToolbarAction(a, t, dispatch, viewState, onViewChange))}
        </div>
      )}
    </div>
  )
}
