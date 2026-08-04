/**
 * ReviewEditor — `review` editor class
 *
 * 流程型(审阅):对 1 个 entity 做 approve / reject + 写批注。状态字段(view_state.statusField,
 * 缺省 'status':pending|approved|rejected)+ 批注字段(noteField,缺省 'note')。
 * approve/reject → entity.update {status}(CAS);批注 blur → entity.update {note}(CAS)。
 * 全程走命令总线(写=Operation),与 editor-architecture「review=approve 流」一致。
 */

import { useEffect, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { ErrorPlaceholder } from '../../lib/Skeleton'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { CompareZone } from '../../zones/CompareZone'
import type { CompareMode } from '../../primitives/compareGeometry'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type ReviewEditorProps = {
  entity: EntityDict | null
  /** 编辑器名(layout slot.nameKey 译名;优先显示槽位名,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  statusField?: string
  noteField?: string
  dispatch?: DispatchFn
  status?: EntityRunStatus
  // ---- W4 前后对比(opt-in):给了 before/after URL 即在顶部渲 CompareFrame ----
  beforeUrl?: string
  afterUrl?: string
  compareMode?: CompareMode
  /** false = 纯对比(隐藏 approve/reject + 批注;如 cleanup 候选)。缺省 true(向后兼容)。 */
  showDecision?: boolean
}

export function ReviewEditor({
  entity, editorName, statusField = 'status', noteField = 'note', dispatch, status,
  beforeUrl, afterUrl, compareMode, showDecision = true,
}: ReviewEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const [note, setNote] = useState('')

  const rawNote = entity && typeof entity.data[noteField] === 'string' ? (entity.data[noteField] as string) : ''
  useEffect(() => { setNote(rawNote) }, [entity?.id, entity?.version])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!entity) {
    return (
      <EditorFrame editorClass="review" header={<HeaderZone title={editorName ?? t('editor.noReview')} />}>
        <EditorFrame.Empty>{t('editor.noReview')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const phase = status?.phase
  const cur = typeof entity.data[statusField] === 'string' ? (entity.data[statusField] as string) : 'pending'
  // draft/in_review = 类型库 workflow_status 词表(07-type-library;SPEC §10 残差兑现)
  const statusLabel: Record<string, string> = {
    pending: t('editor.stPending'), approved: t('editor.stApproved'), rejected: t('editor.stRejected'),
    draft: t('editor.stDraft'), in_review: t('editor.stInReview'),
  }
  const setStatus = (s: string) => {
    if (s === cur) return
    fire('entity.update', { type: entity.type, id: entity.id, patches: { [statusField]: s }, expected_version: entity.version })
  }
  const commitNote = () => {
    if (note === rawNote) return
    fire('entity.update', { type: entity.type, id: entity.id, patches: { [noteField]: note }, expected_version: entity.version })
  }

  const objectName = entity.name || entity.id.slice(0, 8)
  const title = editorName ? `${editorName} · ${objectName}` : objectName
  // 状态徽章 → .ze-badge:approved/rejected 用 solid 实底(绿/红 + 反相字),pending 用默认中性底。
  const badgeAttr: Record<string, { variant?: string; emphasis?: string }> = {
    approved: { variant: 'success', emphasis: 'solid' },
    rejected: { variant: 'danger', emphasis: 'solid' },
  }
  const ba = badgeAttr[cur] ?? {}
  const statusBadge = showDecision ? (
    <span className="ze-badge" data-variant={ba.variant} data-emphasis={ba.emphasis}>
      {statusLabel[cur] ?? cur}
    </span>
  ) : undefined

  if (phase === 'error') {
    return (
      <EditorFrame editorClass="review" header={<HeaderZone title={title} count={statusBadge} />}>
        <div className="ze-review__body ze-stack" data-pad="3" data-run-phase="error"><ErrorPlaceholder error={status?.error} /></div>
      </EditorFrame>
    )
  }

  return (
    <EditorFrame editorClass="review" header={<HeaderZone title={title} count={statusBadge} />}>
      <div className="ze-review__body ze-stack" data-gap="3" data-pad="3">
        <CompareZone beforeUrl={beforeUrl} afterUrl={afterUrl} mode={compareMode} />
        {showDecision && (
          <>
            <div className="ze-row" data-gap="2">
              <button className="ze-button" data-variant="primary" data-size="sm"
                disabled={entity.read_only || cur === 'approved'} onClick={() => setStatus('approved')}>{t('editor.approve')}</button>
              <button className="ze-button" data-variant="secondary" data-size="sm"
                disabled={entity.read_only || cur === 'rejected'} onClick={() => setStatus('rejected')}>{t('editor.reject')}</button>
            </div>
            <label className="ze-text" data-role="secondary">{t('editor.note')}</label>
            <textarea
              className="ze-textarea ze-review__note" data-size="sm" data-rows="4"
              value={note}
              disabled={entity.read_only}
              onChange={(e) => setNote(e.target.value)}
              onBlur={commitNote}
              placeholder={t('editor.notePlaceholder')}
            />
          </>
        )}
      </div>
    </EditorFrame>
  )
}
