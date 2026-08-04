/**
 * PipelineList — 展开主体里"活跃 pipeline"section
 *
 * SPEC §2.3:每条 pipeline 显示 name / stage / progress% + pause/stop/resume,
 * 下方折叠日志可点开。
 *
 * 视觉规则全在 footer.css(.ze-footer__pipelines / .ze-footer__pipeline / 控制按钮)
 *
 * 控制按钮 dispatch:
 *   pause   → pipeline.pause   { pipeline_id }
 *   resume  → pipeline.resume  { pipeline_id }
 *   cancel  → pipeline.cancel  { pipeline_id }
 *
 * Audit Gate(GATE-UI-FOOTER-NO-BUSINESS-MUTATION)只许这 3 个 command。
 */

import { useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import {
  CMD_PIPELINE_CANCEL,
  CMD_PIPELINE_PAUSE,
  CMD_PIPELINE_RESUME,
  CMD_PIPELINE_RETRY_FAILED,
} from './commands'
import { useFooterDispatch } from './lib/dispatch'
import { formatEta } from './lib/formatEta'
import { resolveHonestProgress } from './lib/honestProgress'
import { useScrollAffordance } from '../03-editor/lib/useScrollAffordance'
import { ZeIcon } from '../lib/zeIcon'
import type { PipelineActiveItem, PipelineArtifact, PipelineLogEvent, PipelineRun } from './types'

export type PipelineListProps = {
  pipelines: PipelineRun[]
  /** mod 追加的额外控制按钮(自家 pipeline 自定义);SPEC §2.6 重载点 4 */
  controlsAppend?: (run: PipelineRun) => ReactNode
  /** tab 模式:标题由外层 tab 提供,隐藏自带标题 */
  hideTitle?: boolean
}

export function PipelineList(props: PipelineListProps): ReactNode {
  const { pipelines, controlsAppend, hideTitle } = props
  const t = useT()
  const bodyRef = useScrollAffordance('y')

  return (
    <section className="ze-footer__section" data-flex="pipelines">
      {!hideTitle && (
        <header className="ze-footer__section-header">
          <span>{t('footer.pipelines')}</span>
          <span className="ze-footer__section-header-count">{pipelines.length}</span>
        </header>
      )}
      <div className="ze-footer__section-body ze-scroll-fade-y" ref={bodyRef}>
        {pipelines.length === 0 ? (
          <div className="ze-footer__empty">{t('footer.noPipelines')}</div>
        ) : (
          <div className="ze-footer__pipelines">
            {pipelines.map((run) => (
              <PipelineCard
                key={run.id}
                run={run}
                controlsAppend={controlsAppend}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}


function PipelineCard({
  run,
  controlsAppend,
}: {
  run: PipelineRun
  controlsAppend?: (run: PipelineRun) => ReactNode
}): ReactNode {
  const t = useT()
  const dispatch = useFooterDispatch()
  const [logOpen, setLogOpen] = useState(false)

  const progress = resolveHonestProgress(run.progress, run.status === 'success')
  // 进度条语义色(L2 .ze-progress data-variant):按 run 状态
  const progressVariant =
    run.status === 'paused' ? 'warn'
      : run.status === 'error' ? 'danger'
        : run.status === 'success' ? 'success'
          : run.status === 'cancelled' ? 'cancelled'
            : undefined

  // 控制按钮可见性
  const showPause =
    (run.can_pause ?? true) && run.status === 'running'
  const showResume =
    (run.can_resume ?? true) && run.status === 'paused'
  const showCancel =
    (run.can_cancel ?? true) &&
    (run.status === 'running' || run.status === 'paused')
  // 统一重试:有失败项 + run 已到 error/partial 终态(不在跑)时出现
  const failed = run.failed_count ?? 0
  const showRetry = failed > 0 && (run.status === 'error' || run.status === 'cancelled')
  // P2d 可观测:运行中且有 ETA 样本时显示剩余预估;多 stage run 显示每 stage 完成统计
  const showEta = run.status === 'running' && typeof run.eta_sec === 'number' && run.eta_sec > 0
  const stageStats = run.stages ?? []
  const logEvents = run.log_events ?? []
  const activeItems = run.active_items ?? []
  const artifacts = run.artifacts ?? []
  const hasDetail =
    logEvents.length > 0 || activeItems.length > 0 || artifacts.length > 0 || run.log_lines.length > 0
  const detailCount = logEvents.length || run.log_lines.length

  return (
    <div className="ze-footer__pipeline" data-run-id={run.id} data-status={run.status}>
      <div className="ze-footer__pipeline-header">
        <span className="ze-footer__pipeline-name">{run.name}</span>
        <span className="ze-footer__pipeline-stage">{run.stage}</span>
        <span className="ze-footer__pipeline-percent">
          {progress.indeterminate ? '—' : `${progress.percent}%`}
        </span>
        {showEta && (
          <span className="ze-footer__pipeline-eta" title={t('footer.etaLabel')}>
            ~{formatEta(run.eta_sec as number)}
          </span>
        )}

        <span className="ze-footer__pipeline-controls">
          {showPause && (
            <button
              type="button"
              className="ze-icon-button"
              data-size="xs"
              data-variant="warn"
              title={t('footer.pause')}
              aria-label={t('footer.pause')}
              onClick={() =>
                dispatch(CMD_PIPELINE_PAUSE, { pipeline_id: run.id })
              }
            >
              <ZeIcon name="pause" size="xs" />
            </button>
          )}
          {showResume && (
            <button
              type="button"
              className="ze-icon-button"
              data-size="xs"
              data-variant="success"
              title={t('footer.resume')}
              aria-label={t('footer.resume')}
              onClick={() =>
                dispatch(CMD_PIPELINE_RESUME, { pipeline_id: run.id })
              }
            >
              <ZeIcon name="play" size="xs" />
            </button>
          )}
          {showCancel && (
            <button
              type="button"
              className="ze-icon-button"
              data-size="xs"
              data-variant="danger"
              title={t('footer.cancel')}
              aria-label={t('footer.cancel')}
              onClick={() =>
                dispatch(CMD_PIPELINE_CANCEL, { pipeline_id: run.id })
              }
            >
              <ZeIcon name="stop" size="xs" />
            </button>
          )}
          {showRetry && (
            <button
              type="button"
              className="ze-icon-button"
              data-size="xs"
              title={t('footer.retryFailed', { count: failed })}
              aria-label={t('footer.retryFailedAria')}
              onClick={() =>
                dispatch(CMD_PIPELINE_RETRY_FAILED, { pipeline_id: run.id })
              }
            >
              <ZeIcon name="refresh" size="xs" /> {failed}
            </button>
          )}
          {controlsAppend?.(run)}
        </span>
      </div>

      <span
        className="ze-progress"
        data-variant={progressVariant}
        data-indeterminate={progress.indeterminate ? 'true' : undefined}
        aria-label={t('footer.progressAria')}
        aria-valuenow={progress.ariaValueNow}
      >
        <span
          className="ze-progress__fill"
          style={progress.fillWidth ? { width: progress.fillWidth } : undefined}
        />
      </span>

      {stageStats.length > 0 && (
        <span className="ze-footer__pipeline-stages">
          {stageStats.map((s) => (
            <span
              key={s.stage_id}
              className="ze-footer__pipeline-stage-chip"
              data-complete={s.done >= s.total ? 'true' : 'false'}
            >
              {s.label} {s.done}/{s.total}
            </span>
          ))}
        </span>
      )}

      {hasDetail && (
        <>
          <button
            type="button"
            className="ze-footer__pipeline-log-toggle"
            data-open={logOpen ? 'true' : 'false'}
            onClick={() => setLogOpen((v: boolean) => !v)}
          >
            {logOpen ? t('footer.hideLog') : t('footer.showLog', { count: detailCount })}
          </button>
          {logOpen && (
            <PipelineRunDetail
              run={run}
              logEvents={logEvents}
              activeItems={activeItems}
              artifacts={artifacts}
            />
          )}
        </>
      )}
    </div>
  )
}

function PipelineRunDetail({
  run,
  logEvents,
  activeItems,
  artifacts,
}: {
  run: PipelineRun
  logEvents: PipelineLogEvent[]
  activeItems: PipelineActiveItem[]
  artifacts: PipelineArtifact[]
}): ReactNode {
  const t = useT()
  const logRef = useScrollAffordance('y')
  const detailRef = useScrollAffordance('y')
  if (logEvents.length === 0 && activeItems.length === 0 && artifacts.length === 0) {
    return (
      <pre className="ze-footer__pipeline-log ze-scroll-fade-y" ref={logRef}>
        {run.log_lines.join('\n')}
      </pre>
    )
  }

  return (
    <div className="ze-footer__pipeline-detail ze-scroll-fade-y" ref={detailRef}>
      {activeItems.length > 0 && (
        <section className="ze-footer__pipeline-detail-section">
          <span className="ze-footer__pipeline-detail-title">
            {t('footer.runActiveItems')}
          </span>
          <div className="ze-footer__pipeline-items">
            {activeItems.map((item) => (
              <PipelineItemRow key={item.item_id} item={item} />
            ))}
          </div>
        </section>
      )}

      {logEvents.length > 0 && (
        <section className="ze-footer__pipeline-detail-section">
          <span className="ze-footer__pipeline-detail-title">
            {t('footer.runLogEvents')}
          </span>
          <div className="ze-footer__pipeline-log-events">
            {logEvents.map((event, index) => (
              <PipelineLogRow key={`${event.ts}-${event.item_id ?? ''}-${index}`} event={event} />
            ))}
          </div>
        </section>
      )}

      {artifacts.length > 0 && (
        <section className="ze-footer__pipeline-detail-section">
          <span className="ze-footer__pipeline-detail-title">
            {t('footer.runArtifacts')}
          </span>
          <div className="ze-footer__pipeline-artifacts">
            {artifacts.map((artifact) => (
              <PipelineArtifactRow key={`${artifact.item_id}-${artifact.path}`} artifact={artifact} />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function PipelineItemRow({ item }: { item: PipelineActiveItem }): ReactNode {
  const pct = typeof item.progress === 'number' ? `${Math.round(item.progress * 100)}%` : '—'
  return (
    <div className="ze-footer__pipeline-item" data-phase={item.phase}>
      <span className="ze-footer__pipeline-item-id">{item.item_id}</span>
      <span className="ze-footer__pipeline-item-stage">{item.stage_label || item.phase}</span>
      <span className="ze-footer__pipeline-item-progress">{pct}</span>
      {item.error?.code && (
        <span className="ze-footer__pipeline-item-code">{item.error.code}</span>
      )}
    </div>
  )
}

function PipelineLogRow({ event }: { event: PipelineLogEvent }): ReactNode {
  return (
    <div className="ze-footer__pipeline-log-event" data-level={event.level}>
      <span className="ze-footer__pipeline-log-time">{formatEventTime(event.ts)}</span>
      <span className="ze-footer__pipeline-log-level">{event.level}</span>
      <span className="ze-footer__pipeline-log-locus">
        {event.item_id ? event.item_id : '—'}
        {event.stage_id ? `@${event.stage_id}` : ''}
      </span>
      <span className="ze-footer__pipeline-log-message">{event.message}</span>
      {event.code && <span className="ze-footer__pipeline-log-code">{event.code}</span>}
    </div>
  )
}

function PipelineArtifactRow({ artifact }: { artifact: PipelineArtifact }): ReactNode {
  const t = useT()
  return (
    <div className="ze-footer__pipeline-artifact" data-present={artifact.present ? 'true' : 'false'}>
      <span className="ze-footer__pipeline-artifact-item">{artifact.item_id}</span>
      <span className="ze-footer__pipeline-artifact-path">{artifact.path}</span>
      <span className="ze-footer__pipeline-artifact-state">
        {artifact.present ? t('footer.runArtifactPresent') : t('footer.runArtifactMissing')}
      </span>
    </div>
  )
}

function formatEventTime(ts: number): string {
  const d = new Date(ts < 1_000_000_000_000 ? ts * 1000 : ts)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}
