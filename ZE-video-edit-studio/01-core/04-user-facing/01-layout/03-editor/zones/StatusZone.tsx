/**
 * StatusZone — 状态条(操作区 zone)
 *
 * zone 层门面,实现走 primitives/EditorStatusBar(单行 + collapse 把手 + 瞬时渲染骨架已就位)。
 * 固定骨架:单行布局、永远可见的折叠把手、空活动即不渲染。
 * 拓展点:segments(领域状态段);禁塞业务按钮(职责≠操作条)。
 * 注:log-dock(展开为多行日志面板)是后续阶段变体,本阶段未实现。
 */

import type { ReactNode } from 'react'
import { EditorStatusBar } from '../primitives/EditorStatusBar'
import type { StatusZoneProps } from './types'

export function StatusZone({ running, queued, latestOp, onCancel, segments }: StatusZoneProps): ReactNode {
  return (
    <EditorStatusBar
      running={running} queued={queued} latestOp={latestOp} onCancel={onCancel} segments={segments}
    />
  )
}
