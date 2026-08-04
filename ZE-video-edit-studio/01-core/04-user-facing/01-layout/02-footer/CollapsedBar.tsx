/**
 * CollapsedBar — Footer 折叠态 24px segment 行
 *
 * SPEC §2.2 折叠态默认显示
 *
 * 视觉规则全在 footer.css(.ze-footer__bar / .ze-footer__segment / .ze-footer__dot)
 * 本组件只决定数据 → class + data-* 的映射。
 */

import { type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../lib/zeIcon'
import type { OpLogEntry, PersistenceState, ServerStatus, SyncState } from './types'

type TFn = ReturnType<typeof useT>

export type CollapsedBarProps = {
  serverStatus: ServerStatus
  skillName: string
  sync: SyncState
  persistence: PersistenceState
  errorCount: number
  /** [v4] 折叠条左侧「最近一次操作」事件行(op log 第一条);无则不显 */
  latestOp?: OpLogEntry
  traceIdShort?: string
  activePipelineCount: number
  /** 取头一个活跃 pipeline 在 segment 显微进度;无则不显 */
  topPipelineProgress?: number
  hideServer?: boolean
  hideSkillName?: boolean
  hideSync?: boolean
  hideProgress?: boolean
  hideErrors?: boolean
  hideTraceId?: boolean
  segmentAppend?: ReactNode
}

export function CollapsedBar(props: CollapsedBarProps): ReactNode {
  const {
    serverStatus,
    skillName,
    sync,
    persistence,
    errorCount,
    latestOp,
    traceIdShort,
    activePipelineCount,
    topPipelineProgress,
    hideServer,
    hideSkillName,
    hideSync,
    hideProgress,
    hideErrors,
    hideTraceId,
    segmentAppend,
  } = props
  const t = useT()

  const showSync = !hideSync && sync.kind !== 'unavailable' && sync.kind !== 'in-sync'
  const syncLabel = showSync ? syncKindToLabel(sync, t) : ''
  const showPersistence = persistence.kind !== 'unavailable'
  const persistenceLabel = persistenceKindToLabel(persistence, t)
  const persistenceTone = persistenceKindToTone(persistence)
  const errorState =
    errorCount === 0 ? 'normal' : errorCount < 5 ? 'warn' : 'danger'

  return (
    <div className="ze-footer__bar">
      {/* [v4] 语义分区:左 = 刚发生了什么(op 行),右 = 系统现在怎么样(既有 segments)。
          视觉语言对齐 OpLogPanel:source/status 用机器 token(mono),i18n 文案进 title。 */}
      {latestOp && (
        <>
          <span
            className="ze-footer__op"
            title={
              t('footer.sourceLabel', { source: latestOp.source }) +
              ' · ' +
              t('footer.statusLabel', { status: latestOp.status }) +
              (latestOp.error_message ? `\n${latestOp.error_message}` : '')
            }
          >
            <em>{latestOp.source}</em>
            <b>{latestOp.command_label}</b>
            <span>
              → {latestOp.status === 'success'
                ? formatOpDuration(latestOp.duration_ms)
                : latestOp.error_message ?? latestOp.status}
            </span>
          </span>
          <span className="ze-spacer" aria-hidden />
        </>
      )}

      {!hideServer && (
        <span
          className="ze-footer__segment"
          data-segment="server"
          title={t('footer.serverPrefix', { status: t(`footer.server.${serverStatus}`) })}
        >
          <span className="ze-footer__dot" data-state={serverStatus} />
          <span className="ze-footer__segment-label">{t(`footer.server.${serverStatus}`)}</span>
        </span>
      )}

      {!hideServer && !hideSkillName && (
        <span className="ze-footer__segment-divider" aria-hidden />
      )}

      {!hideSkillName && (
        <span className="ze-footer__segment" data-segment="skill">
          <span className="ze-footer__segment-value">{skillName}</span>
        </span>
      )}

      {showPersistence && (
        <>
          <span className="ze-footer__segment-divider" aria-hidden />
          <span
            className="ze-footer__segment"
            data-segment="persistence"
            data-state={persistenceTone.segment}
            title={t('persistence.prefix', { label: persistenceLabel })}
          >
            <span className="ze-footer__dot" data-state={persistenceTone.dot} />
            <span className="ze-footer__segment-value">{persistenceLabel}</span>
          </span>
        </>
      )}

      {showSync && (
        <>
          <span className="ze-footer__segment-divider" aria-hidden />
          <span
            className="ze-footer__segment"
            data-segment="sync"
            title={t('footer.syncPrefix', { label: syncLabel })}
          >
            <span className="ze-footer__dot" data-state={sync.kind} />
            <span className="ze-footer__segment-label">{syncLabel}</span>
          </span>
        </>
      )}

      {!hideProgress && activePipelineCount > 0 && (
        <>
          <span className="ze-footer__segment-divider" aria-hidden />
          <span
            className="ze-footer__segment"
            data-segment="progress"
            data-priority="low"
            title={t('footer.activePipelines', { count: activePipelineCount })}
          >
            <span className="ze-footer__segment-label">
              <ZeIcon name="play" size="xs" /> {t('footer.tasks', { count: activePipelineCount })}
            </span>
            {typeof topPipelineProgress === 'number' && (
              <span
                className="ze-progress"
                data-size="sm"
                style={{ width: 80 }}
                aria-label={t('footer.progressAria')}
                aria-valuenow={Math.round(topPipelineProgress * 100)}
              >
                <span
                  className="ze-progress__fill"
                  style={{ width: `${Math.round(topPipelineProgress * 100)}%` }}
                />
              </span>
            )}
          </span>
        </>
      )}

      {!hideErrors && (
        <>
          <span className="ze-footer__segment-divider" aria-hidden />
          <span
            className="ze-footer__segment"
            data-segment="errors"
            data-state={errorState}
            data-priority="low"
            title={t('footer.errorsSince', { count: errorCount })}
          >
            <span className="ze-footer__segment-label ze-icon" data-icon="warn" data-size="xs" aria-hidden="true" />
            <span className="ze-footer__segment-value">{errorCount}</span>
          </span>
        </>
      )}

      {!hideTraceId && traceIdShort && (
        <>
          <span className="ze-footer__segment-divider" aria-hidden />
          <button
            type="button"
            className="ze-footer__segment"
            data-segment="trace"
            data-interactive="true"
            data-priority="low"
            title={t('footer.tracePrefix', { id: traceIdShort })}
            onClick={async (e) => {
              e.stopPropagation()
              try {
                await navigator.clipboard?.writeText(traceIdShort)
              } catch {
                /* ignore */
              }
            }}
          >
            <span className="ze-footer__trace">#{traceIdShort}</span>
          </button>
        </>
      )}

      {segmentAppend && (
        <>
          <span className="ze-footer__segment-divider" aria-hidden />
          {segmentAppend}
        </>
      )}
    </div>
  )
}


/** op 行耗时(与 OpLogPanel.formatDuration 同规则的折叠条精简版) */
function formatOpDuration(ms: number): string {
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60_000).toFixed(1)}min`
}


function syncKindToLabel(sync: SyncState, t: TFn): string {
  switch (sync.kind) {
    case 'unavailable':
      return t('sync.unavailable')
    case 'in-sync':
      return t('sync.inSync')
    case 'syncing':
      return t('footer.syncingCount', { count: sync.pending_op_count })
    case 'delayed':
      return t('sync.delayed')
    case 'conflict':
      return t('footer.conflictCount', { count: sync.conflict_count })
    case 'offline':
      return t('sync.offline')
  }
}

function persistenceKindToLabel(persistence: PersistenceState, t: TFn): string {
  switch (persistence.kind) {
    case 'unavailable':
      return t('persistence.unavailable')
    case 'clean':
      return t('persistence.clean')
    case 'committing':
      return t('persistence.committing')
    case 'dirty':
      return t('persistence.dirty')
    case 'failed':
      return t('persistence.failed')
    case 'repair-required':
      return t('persistence.repairRequired')
  }
}

/** 只映射到 footer 已有视觉状态；不让持久化语义复用 SyncState 类型本身。 */
function persistenceKindToTone(persistence: PersistenceState): {
  dot: 'in-sync' | 'syncing' | 'delayed' | 'conflict'
  segment: 'normal' | 'warn' | 'danger'
} {
  switch (persistence.kind) {
    case 'unavailable':
    case 'clean':
      return { dot: 'in-sync', segment: 'normal' }
    case 'committing':
      return { dot: 'syncing', segment: 'normal' }
    case 'dirty':
      return { dot: 'delayed', segment: 'warn' }
    case 'failed':
    case 'repair-required':
      return { dot: 'conflict', segment: 'danger' }
  }
}
