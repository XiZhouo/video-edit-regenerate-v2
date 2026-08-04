/**
 * SyncIndicator — § 2.3.4 同步状态指示
 *
 * 显示规则:
 *   - unavailable/in-sync(默认) → 不显
 *   - syncing → 蓝色 ●
 *   - delayed → 黄色 ●
 *   - conflict → 红色 ●(脉冲动画)
 *   - offline → 走 Footer 服务器状态 segment,不进本控件
 *
 * 点击 → 跳 Footer 展开"同步详情"面板(批 4 用 hook 提供 jumpToFooter)
 */

import { type ReactNode } from 'react'

import { useT, type I18nKey } from '@ze/core/04-user-facing/05-preferences'
import type { SyncState } from '../types'

export type SyncIndicatorProps = {
  sync: SyncState
}

const LABEL_KEY: Record<SyncState['kind'], I18nKey> = {
  'unavailable': 'sync.unavailable',
  'in-sync': 'sync.inSync',
  'syncing': 'sync.syncing',
  'delayed': 'sync.delayed',
  'conflict': 'sync.conflict',
  'offline': 'sync.offline',
}

export function SyncIndicator(props: SyncIndicatorProps): ReactNode {
  const t = useT()
  // SPEC §2.3.4:in-sync 不显;offline 走 Footer 服务器状态
  if (props.sync.kind === 'unavailable') return null
  if (props.sync.kind === 'in-sync') return null
  if (props.sync.kind === 'offline') return null

  const handleClick = () => {
    // 批 4 hook 提供 jumpToFooter;批 2 console
    // eslint-disable-next-line no-console
    console.info('[ZE Header] jump to Footer sync details (batch 4 hook will wire)')
  }

  return (
    <button
      type="button"
      className="ze-header__sync-indicator"
      data-state={props.sync.kind}
      aria-label={t(LABEL_KEY[props.sync.kind])}
      title={t(LABEL_KEY[props.sync.kind])}
      onClick={handleClick}
    />
  )
}
