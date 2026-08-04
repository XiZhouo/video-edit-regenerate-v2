/**
 * CompareEditor — `compare` editor class(前后对比 viewer;一等公民,与 review 切清)
 *
 * 产品语义是「对比/查看」,不是「审阅/采纳」—— 故独立于 ReviewEditor:**无** approve/reject /
 * 状态徽章 / 批注,只呈现 CompareFrame(并排 / 滑块 / 叠加)。单实体的 before/after 资产字段
 * (view_state.beforeField/afterField)经 slotAdapters 解析为 URL 传入。纯展示、零写库。
 *
 * 由 V3-MIGRATION-GAPS #1 驱动:此前 4 个 skill 用 `review(showDecision:false)` 假冒 compare,
 * 让 review 背上对比专属 props。本编辑器把对比独立出来,review 回归纯审批。
 */

import { type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ErrorPlaceholder } from '../../lib/Skeleton'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { CompareFrame } from '../../primitives/CompareFrame'
import type { CompareMode } from '../../primitives/compareGeometry'
import type { EntityDict, EntityRunStatus } from '../../types'

export type CompareEditorProps = {
  entity: EntityDict | null
  /** 编辑器名(layout slot.nameKey 译名;优先显示槽位名,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  beforeUrl?: string
  afterUrl?: string
  beforeLabel?: string
  afterLabel?: string
  mode?: CompareMode
  status?: EntityRunStatus
}

export function CompareEditor({
  entity, editorName, beforeUrl, afterUrl, beforeLabel, afterLabel, mode, status,
}: CompareEditorProps): ReactNode {
  const t = useT()
  if (!entity) {
    return (
      <EditorFrame editorClass="compare" header={<HeaderZone title={editorName ?? t('editor.noCompare')} />}>
        <EditorFrame.Empty>{t('editor.noCompare')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }
  const objectName = entity.name || entity.id.slice(0, 8)
  const title = editorName ? `${editorName} · ${objectName}` : objectName
  if (status?.phase === 'error') {
    return (
      <EditorFrame editorClass="compare" header={<HeaderZone title={title} />}>
        <div className="ze-compare__body ze-stack" data-pad="3" data-run-phase="error">
          <ErrorPlaceholder error={status?.error} />
        </div>
      </EditorFrame>
    )
  }
  return (
    <EditorFrame editorClass="compare" header={<HeaderZone title={title} />}>
      <div className="ze-compare__body ze-stack" data-gap="3" data-pad="3">
        <CompareFrame
          beforeUrl={beforeUrl} afterUrl={afterUrl}
          beforeLabel={beforeLabel} afterLabel={afterLabel} mode={mode}
        />
      </div>
    </EditorFrame>
  )
}
