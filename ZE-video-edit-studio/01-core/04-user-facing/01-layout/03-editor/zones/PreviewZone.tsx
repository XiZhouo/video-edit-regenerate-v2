/**
 * PreviewZone — 单对象预览内容区(functional zone;SPEC §2B `preview`)
 *
 * 固定骨架(组件画死):canvas-body 容器 + 运行态(error→占位可重试)+ 处理中覆盖层 +
 * 右键钩子。**预览内容本身(图片视口 / 3D 渲染器 / 视频播放器 / 文件信息 / 空态)由编辑器
 * 作为 children 传入** —— 那是编辑器的主数据,各类差异大,zone 只统一外壳。
 * 复用现 `.ze-editor__canvas-body`(editor.css;CSS 暂留 L4,Phase 4 去令牌)。
 */

import { type ReactNode, type MouseEvent } from 'react'
import { ErrorPlaceholder } from '../lib/Skeleton'
import { CanvasBusyOverlay } from '../lib/CanvasBusyOverlay'
import type { EntityRunStatus, RunError } from '../types'

export type PreviewZoneProps = {
  phase?: string
  error?: RunError
  onRetry?: () => void
  /** 处理中覆盖层数据(走 CanvasBusyOverlay,自身判断是否显示)。 */
  busyStatus?: EntityRunStatus
  /** 是否挂处理中覆盖层(scene3d 仅渲染器挂载时;asset/media 恒 true)。缺省 true。 */
  showBusy?: boolean
  onContextMenu?: (e: MouseEvent) => void
  children: ReactNode
}

export function PreviewZone({
  phase, error, onRetry, busyStatus, showBusy = true, onContextMenu, children,
}: PreviewZoneProps): ReactNode {
  return (
    <div className="ze-editor__canvas-body" data-run-phase={phase} onContextMenu={onContextMenu}>
      {phase === 'error' ? (
        <ErrorPlaceholder error={error} onRetry={onRetry} />
      ) : (
        <>
          {children}
          {showBusy && <CanvasBusyOverlay status={busyStatus} />}
        </>
      )}
    </div>
  )
}
