/**
 * renderToolbarAction — 操作件渲染(ToolbarZone / ToolRowZone / ControlZone 共享)
 *
 * ToolbarAction 的四种受约束语义(command/toggle/search/cycle)统一在此渲染,各 zone 复用,
 * 避免逐 zone 重写。禁裸 onClick/JSX —— 业务语义全在声明里(command→dispatch,
 * toggle/search/cycle→viewState)。原 ToolbarZone.renderAction 抽出,行为不变。
 */

import type { ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { IconOrGlyph } from '../../lib/zeIcon'
import type { ToolbarAction, ToolbarZoneProps } from './types'

export function renderToolbarAction(
  a: ToolbarAction,
  t: ReturnType<typeof useT>,
  dispatch: ToolbarZoneProps['dispatch'],
  viewState: ToolbarZoneProps['viewState'],
  onViewChange: ToolbarZoneProps['onViewChange'],
): ReactNode {
  const label = t(a.labelKey)
  if (a.kind === 'search') {
    const key = a.viewKey ?? 'query'
    const cur = String(viewState?.[key] ?? '')
    return (
      <input
        key={a.id}
        className="ze-input ze-editor__search" data-size="sm" type="search"
        placeholder={label} aria-label={label}
        value={cur} onChange={(e) => onViewChange?.(key, e.target.value)}
      />
    )
  }
  if (a.kind === 'cycle') {
    const key = a.viewKey ?? a.id
    const options = a.options ?? []
    const cur = String(viewState?.[key] ?? options[0] ?? '')
    const next = options[(options.indexOf(cur) + 1) % Math.max(options.length, 1)]
    return (
      <button
        key={a.id} type="button" className="ze-button" data-size="sm" data-variant="ghost"
        title={label} aria-label={label} disabled={a.disabled || options.length === 0}
        data-kind="cycle"
        onClick={() => onViewChange?.(key, next)}
      >
        {a.icon && <IconOrGlyph value={a.icon} />}<span className="ze-button__label">{cur.toUpperCase()}</span>
      </button>
    )
  }
  if (a.kind === 'toggle') {
    const key = a.viewKey ?? a.id
    const on = Boolean(viewState?.[key])
    return (
      <button
        key={a.id} type="button" className="ze-button" data-size="sm"
        data-variant={on ? (a.variant ?? 'primary') : 'ghost'} data-active={on ? 'true' : undefined}
        aria-pressed={on} disabled={a.disabled}
        onClick={() => onViewChange?.(key, !on)}
      >
        {a.icon && <IconOrGlyph value={a.icon} />}<span className="ze-button__label">{label}</span>
      </button>
    )
  }
  // kind === 'command'
  return (
    <button
      key={a.id} type="button" className="ze-button" data-size="sm" data-variant={a.variant}
      data-ze-cmd={a.command}
      disabled={a.disabled}
      onClick={() => { if (a.command) dispatch?.(a.command, a.args ?? {}) }}
    >
      {a.icon && <IconOrGlyph value={a.icon} />}<span className="ze-button__label">{label}</span>
    </button>
  )
}
