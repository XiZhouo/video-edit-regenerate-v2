/**
 * MiddleSegment — Header 中段
 *
 * SPEC: ./SPEC.md §2.3.3 项目名 + §2.3.4 同步指示 + §2.3.5 文件切换器
 *
 * [header-v2] 中段 = 文件切换器(主角,栅格 1fr auto 1fr 下绝对居中):
 *   - 文件名 = FileSwitcher trigger:点击开下拉(文件列表 + 行内操作 + 其他文件操作);
 *     无 doc 显占位仍可开列表。重命名不再挂在项目名点击上 —— 走列表行内 ✎ 按钮
 *     (5.8 入口变更,红线与 dialog 不变)。
 *   - .ze-header__sync-dot / __sync-label:仅在同步中、延迟或冲突时显示;默认 unavailable/in-sync/offline 不占 Header。
 *
 * mod 可通过 `slot` 注入业务自定义(挂同步状态右侧)。
 */

import { type ReactNode } from 'react'

import { useT, type I18nKey } from '@ze/core/04-user-facing/05-preferences'
import { FileSwitcher } from './menus/FileSwitcher'
import type { DocumentInfo, HeaderFileItem, SyncState } from './types'

export type MiddleSegmentProps = {
  doc: DocumentInfo
  sync: SyncState
  /** 文件切换器数据(header-v2;App 从 03-authentication 注入) */
  projects?: HeaderFileItem[]
  recentIds?: string[]
  recentMax?: number
  onRefreshProjects?: () => void
  /** 示例场景模板(文件切换器示例区) */
  demoTemplates?: { id: string; name: string; sandbox_generation: number }[]
  isDemoSession?: boolean
  /** mod 在文件下拉操作区末尾追加(SPEC §2.6 重载点 2) */
  fileMenuAppend?: ReactNode
  hideRename?: boolean
  hideDelete?: boolean
  /** mod 业务自定义(挂同步状态右) */
  slot?: ReactNode
}

const SYNC_LABEL_KEY: Record<SyncState['kind'], I18nKey> = {
  'unavailable': 'sync.unavailable',
  'in-sync': 'sync.inSync',
  'syncing': 'sync.syncing',
  'delayed': 'sync.delayed',
  'conflict': 'sync.conflict',
  'offline': 'sync.offline',
}

export function MiddleSegment(props: MiddleSegmentProps): ReactNode {
  const t = useT()
  const showSyncStatus = props.sync.kind !== 'unavailable' && props.sync.kind !== 'in-sync' && props.sync.kind !== 'offline'

  return (
    <div className="ze-header__middle" data-ze-segment="middle">
      <FileSwitcher
        doc={props.doc}
        sync={props.sync}
        projects={props.projects}
        recentIds={props.recentIds}
        recentMax={props.recentMax}
        onRefresh={props.onRefreshProjects}
        demoTemplates={props.demoTemplates}
        isDemoSession={props.isDemoSession}
        append={props.fileMenuAppend}
        hideRename={props.hideRename}
        hideDelete={props.hideDelete}
      />

      {showSyncStatus ? (
        <>
          <span
            className="ze-header__sync-dot"
            data-state={props.sync.kind}
            aria-hidden="true"
          />
          <span className="ze-header__sync-label" title={t(SYNC_LABEL_KEY[props.sync.kind])}>
            {t(SYNC_LABEL_KEY[props.sync.kind])}
          </span>
        </>
      ) : null}

      {props.slot}
    </div>
  )
}
