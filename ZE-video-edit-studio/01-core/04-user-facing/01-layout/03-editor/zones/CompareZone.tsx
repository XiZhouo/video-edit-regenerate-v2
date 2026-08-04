/**
 * CompareZone — 前后对比内容区(functional zone;SPEC §2B `compare`)
 *
 * 固定骨架(组件画死):对比容器 + CompareFrame(并排 / 滑块 / 叠加,交互在 primitive 内)。
 * 给了 before/after 任一 URL 才渲染,否则返回 null(编辑器据此决定是否摆这块)。
 * 复用现 `.ze-review__compare`(review.css;CSS 暂留 L4,Phase 4 去令牌)。
 */

import { type ReactNode } from 'react'
import { CompareFrame } from '../primitives/CompareFrame'
import type { CompareMode } from '../primitives/compareGeometry'

export type CompareZoneProps = {
  beforeUrl?: string
  afterUrl?: string
  mode?: CompareMode
  beforeLabel?: string
  afterLabel?: string
}

export function CompareZone({ beforeUrl, afterUrl, mode, beforeLabel, afterLabel }: CompareZoneProps): ReactNode {
  if (!beforeUrl && !afterUrl) return null
  return (
    <div className="ze-review__compare">
      <CompareFrame beforeUrl={beforeUrl} afterUrl={afterUrl} mode={mode} beforeLabel={beforeLabel} afterLabel={afterLabel} />
    </div>
  )
}
