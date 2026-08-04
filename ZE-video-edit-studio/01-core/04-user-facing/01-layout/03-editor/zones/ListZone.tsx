/**
 * ListZone — 单列行式列表内容区(functional zone;SPEC §2B `list`)
 *
 * 固定骨架(组件画死):`.ze-editor__task-body` 滚动容器 + 等高任务行 + 空态/无匹配态。
 * 每行 = 主名 + 分阶段进度 pills(stagesField 字段映射)+ 状态指示(error 徽章+重试 / running
 * 进度环+取消 / 其它 状态徽章+运行)。**行内状态/进度一律内联,不撑高行**(语义同类行严格同高,
 * 见 editor-architecture 行高系统)。复用现 `.ze-task-row*`/`.ze-editor__task-body`(task.css;CSS 暂留 L4)。
 *
 * 拓展点(受约束):`stagesField`(字段映射,→ resolveStages 渲染 pills)+ `onRun`(mod 提供的
 * 运行动作)+ builtin pipeline 重试/取消(经 dispatch)。过滤由编辑器持有(filter 段控在 toolbar),
 * 本 zone 只渲染传入的 `rows`;`totalCount` 区分"无任务"与"无匹配"两种空态。
 */

import { type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ProgressRing } from '../lib/Skeleton'
import { ZeIcon } from '../../lib/zeIcon'
import { resolveStages, statusOfRow, rowProgress, isKnownStatus } from '../lib/taskStages'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../types'

export type ListZoneProps = {
  /** 已按编辑器过滤后的待渲染行(顺序即展示顺序)。 */
  rows: EntityDict[]
  /** 过滤前总数:0 → 显示"无任务";>0 但 rows 空 → 显示"无匹配"。 */
  totalCount: number
  selectedId?: string | null
  onSelect?: (id: string) => void
  statusOf?: (id: string) => EntityRunStatus | undefined
  /** mod 提供的"运行"动作(dispatch 自家生成命令);无则不显示运行按钮。 */
  onRun?: (id: string) => void
  /** 分阶段进度字段名(每项 {label/name, status?, progress?})。 */
  stagesField?: string
  dispatch?: DispatchFn
}

export function ListZone({
  rows, totalCount, selectedId, onSelect, statusOf, onRun, stagesField, dispatch,
}: ListZoneProps): ReactNode {
  const t = useT()
  const statusLabel = (s: string) => (isKnownStatus(s) ? t(`task.status.${s}`) : s)
  const bodyRef = useScrollAffordance('y')

  return (
    <div ref={bodyRef} className="ze-editor__task-body ze-scroll-fade-y ze-gap ze-pad" data-gap="1" data-p="1">
      {totalCount === 0 ? (
        <div className="ze-editor__empty">{t('editor.noTask')}</div>
      ) : rows.length === 0 ? (
        <div className="ze-editor__empty">{t('editor.noMatch')}</div>
      ) : rows.map((e) => {
        const st = statusOf?.(e.id)
        const phase = st?.phase
        const status = statusOfRow(e, st)
        const stage = st?.stage_label || (e.data.stage as string | undefined) || ''
        const prog = rowProgress(e, st)
        const stages = resolveStages(st, e, stagesField)
        const isRunning = phase === 'running' || status === 'running'

        return (
          <div
            key={e.id}
            className="ze-task-row ze-gap ze-pad ze-round ze-surface ze-size ze-row-card"
            data-gap="2" data-py="2" data-px="3" data-radius="md" data-surface="raised" data-minh="8"
            data-status={status}
            data-selected={e.id === selectedId ? 'true' : 'false'}
            onClick={() => onSelect?.(e.id)}
          >
            <span className="ze-task-row__name ze-text" data-weight="medium">{e.name || e.id.slice(0, 8)}</span>
            {stages.length > 0 ? (
              <span className="ze-task-row__stages ze-gap" data-gap="1">
                {stages.map((s, i) => {
                  const pillTone = s.status === 'done' ? 'success' : s.status === 'error' ? 'danger' : s.status === 'running' ? 'accent' : undefined
                  return (
                  <span key={i} className="ze-task-row__stage-pill ze-gap ze-round ze-surface ze-pad ze-text" data-gap="0-5" data-radius="pill" data-surface="sunken" data-px="2" data-role="secondary" data-size="xs" data-tone={pillTone} title={s.label}>
                    {s.status === 'running'
                      ? <ProgressRing progress={s.progress} size={14} />
                      : <span className="ze-task-row__stage-dot ze-icon" data-icon={s.status === 'done' ? 'check' : s.status === 'error' ? 'close' : 'dot'} data-size="xs" aria-hidden="true" />}
                    <span className="ze-task-row__stage-label ze-size" data-maxw="16">{s.label}</span>
                  </span>
                  )
                })}
              </span>
            ) : stage && <span className="ze-text" data-role="tertiary">{stage}</span>}
            {phase === 'error' ? (
              <>
                <span className="ze-badge" data-variant="danger" data-size="sm">{statusLabel('error')}</span>
                {st?.run_id && (
                  <button type="button" className="ze-icon-button" data-size="sm" data-variant="ghost" title={t('editor.retry')}
                    onClick={(ev) => { ev.stopPropagation(); dispatch?.('pipeline.retry_failed', { pipeline_id: st.run_id, item_ids: [e.id] }) }}><ZeIcon name="refresh" size="xs" /></button>
                )}
              </>
            ) : isRunning ? (
              <>
                <ProgressRing progress={prog} size={20} />
                {st?.run_id && (
                  <button type="button" className="ze-button" data-size="sm" data-variant="ghost"
                    onClick={(ev) => { ev.stopPropagation(); dispatch?.('pipeline.cancel', { pipeline_id: st.run_id }) }}>{t('editor.cancel')}</button>
                )}
              </>
            ) : (
              <>
                <span className="ze-badge" data-variant={status === 'done' ? 'success' : 'default'} data-size="sm">{statusLabel(status)}</span>
                {onRun && (
                  <button type="button" className="ze-button" data-size="sm" data-variant="ghost"
                    onClick={(ev) => { ev.stopPropagation(); onRun(e.id) }}>{t('editor.run')}</button>
                )}
              </>
            )}
          </div>
        )
      })}
    </div>
  )
}
