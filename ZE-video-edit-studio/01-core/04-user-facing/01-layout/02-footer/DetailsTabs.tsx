/**
 * DetailsTabs — 展开主体里"详情"section,三 tab(可加)
 *
 * SPEC §2.3 第三 section + §2.6 重载点 3(mod 追加 tab)
 *
 * 视觉规则全在 footer.css(.ze-footer__tabs / .ze-footer__tab / .ze-footer__kv-list ...)
 */

import { useMemo, useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import type { ErrorEntry, SyncState, TraceLink } from './types'

export type DetailsTab = {
  id: string
  label: string
  count?: number
  variant?: 'default' | 'danger'
  render: () => ReactNode
}

export type DetailsTabsProps = {
  sync: SyncState
  errors: ErrorEntry[]
  traceLink: TraceLink | null
  /** mod 追加 tab(SPEC §2.6 重载点 3)*/
  tabsAppend?: Array<DetailsTab>
}

export function DetailsTabs(props: DetailsTabsProps): ReactNode {
  const { sync, errors, traceLink, tabsAppend } = props
  const t = useT()

  const tabs: DetailsTab[] = useMemo(() => {
    const base: DetailsTab[] = [
      ...(sync.kind === 'unavailable'
        ? []
        : [{
            id: 'sync',
            label: t('footer.tabSync'),
            render: () => <SyncDetail sync={sync} />,
          }]),
      {
        id: 'errors',
        label: t('footer.tabErrors'),
        count: errors.length,
        variant: errors.length > 0 ? 'danger' : 'default',
        render: () => <ErrorList errors={errors} />,
      },
      {
        id: 'trace',
        label: t('footer.tabTrace'),
        render: () => <TraceChain link={traceLink} />,
      },
    ]
    return [...base, ...(tabsAppend ?? [])]
  }, [sync, errors, traceLink, tabsAppend, t])

  const [activeId, setActiveId] = useState(tabs[0].id)
  const active = tabs.find((t) => t.id === activeId) ?? tabs[0]

  return (
    <section className="ze-footer__section" data-flex="details">
      <div className="ze-tabs" data-density="compact">
        <header className="ze-tabs__headers" role="tablist">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              className="ze-tab"
              data-active={tab.id === activeId ? 'true' : 'false'}
              data-variant={tab.variant ?? undefined}
              aria-selected={tab.id === activeId}
              onClick={() => setActiveId(tab.id)}
            >
              <span>{tab.label}</span>
              {typeof tab.count === 'number' && tab.count > 0 && (
                <span className="ze-tab__count">{tab.count}</span>
              )}
            </button>
          ))}
        </header>
        <div className="ze-tabs__body" role="tabpanel">
          {active.render()}
        </div>
      </div>
    </section>
  )
}


// ============================================================
// Tab 内部内容
// ============================================================

function SyncDetail({ sync }: { sync: SyncState }): ReactNode {
  const t = useT()
  const statusKey = t('footer.statusField')
  const rows: Array<[string, string]> = (() => {
    switch (sync.kind) {
      case 'unavailable':
        return [[statusKey, t('sync.unavailable')]]
      case 'in-sync':
        return [[statusKey, t('sync.inSync')]]
      case 'syncing':
        return [
          [statusKey, t('sync.syncing')],
          [t('footer.pendingOps'), String(sync.pending_op_count)],
        ]
      case 'delayed':
        return [
          [statusKey, t('sync.delayed')],
          [t('footer.delayMs'), String(sync.delay_ms)],
          [t('footer.pendingOps'), String(sync.pending_op_count ?? 0)],
        ]
      case 'conflict':
        return [
          [statusKey, t('footer.conflictShort')],
          [t('footer.conflicts'), String(sync.conflict_count)],
          [t('footer.traceId'), sync.trace_ids.join(', ') || '-'],
        ]
      case 'offline':
        return [[statusKey, t('sync.offline')]]
    }
  })()

  return (
    <div className="ze-kv-list" style={{ padding: 'var(--ze-space-2) var(--ze-space-3)' }}>
      {rows.map(([k, v]) => (
        <div key={k} className="ze-kv-list__row">
          <span className="ze-kv-list__key">{k}</span>
          <span className="ze-kv-list__value" data-monospace="true">{v}</span>
        </div>
      ))}
    </div>
  )
}


function ErrorList({ errors }: { errors: ErrorEntry[] }): ReactNode {
  const t = useT()
  if (errors.length === 0) {
    return <div className="ze-footer__empty">{t('footer.noErrors')}</div>
  }

  return (
    <div className="ze-footer__errors">
      {errors.map((err) => (
        <button
          key={err.id}
          type="button"
          className="ze-footer__error-entry"
          data-level={err.level}
          title={t('footer.errRowTitle', { msg: err.message, trace: err.trace_id })}
        >
          <span className="ze-footer__error-time">
            {formatTime(err.timestamp)}
          </span>
          <span className="ze-footer__error-level">{err.level}</span>
          <span className="ze-footer__error-message">{err.message}</span>
          <span className="ze-footer__error-trace">#{err.trace_id.slice(0, 6)}</span>
        </button>
      ))}
    </div>
  )
}


function TraceChain({ link }: { link: TraceLink | null }): ReactNode {
  const t = useT()
  if (!link || link.layers.length === 0) {
    return <div className="ze-footer__empty">{t('footer.noTrace')}</div>
  }

  return (
    <div className="ze-footer__trace-chain">
      <div className="ze-kv-list__row">
        <span className="ze-kv-list__key">{t('footer.traceId')}</span>
        <span className="ze-kv-list__value" data-monospace="true">{link.trace_id}</span>
      </div>
      {link.layers.map((layer) => (
        <div key={`${layer.layer}-${layer.timestamp}`} className="ze-footer__trace-layer">
          <span className="ze-footer__trace-layer-marker" aria-hidden />
          <span className="ze-footer__trace-layer-name">{layer.layer}</span>
          <span className="ze-footer__trace-layer-detail">
            {layer.detail ?? '—'}
          </span>
          <span className="ze-footer__trace-layer-duration">
            {typeof layer.duration_ms === 'number'
              ? `${layer.duration_ms}ms`
              : '—'}
          </span>
        </div>
      ))}
    </div>
  )
}


function formatTime(ms: number): string {
  const d = new Date(ms)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}
