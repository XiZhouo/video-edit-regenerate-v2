/**
 * FileSwitcher — 中段文件名下拉(header-v2,替代原左段 FileMenu)
 *
 * SPEC: ../SPEC.md §2.3.5 文件切换器
 *
 * trigger = 当前文件名本体(无 doc 显占位,仍可开列表)。popover 两区:
 *   - 文件列表区:LRU 优先 + updated_at 倒序(lib/fileListModel.mergeFileList);
 *     点击行 = dispatch document.open;行 hover/focus 出行内按钮组
 *     (rename/duplicate/save_as/delete,icon 令牌 + aria-label;gate 见 rowActions)
 *   - 操作区:新建 / 导入 / 导出 / 创建快照 / 关闭 + mod append(重载点 2 落点)
 *
 * 打开瞬间 onRefresh?() 拉新列表(无 SSE 广播,开即拉,SPEC 边界)。
 * 删除红线:行内删除仍 openDialog('delete', {target})(手打全名,恒 true)。
 */

import { useEffect, type MouseEvent, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useHeaderDispatch } from '../lib/dispatch'
import { useHeaderDialog, type DialogKind } from '../dialogs'
import { usePopover } from '../lib/usePopover'
import { useScrollAffordance } from '../../03-editor/lib/useScrollAffordance'
import { mergeFileList, rowActions, rowActionTarget, type RowAction } from '../lib/fileListModel'
import { MenuPopover } from './MenuPopover'
import { MenuItem, MenuGroup } from './MenuItem'
import { ZeIcon } from '../../lib/zeIcon'
import { CMD_DOCUMENT_OPEN, CMD_DOCUMENT_DUPLICATE, CMD_DEMO_OPEN } from '../commands'
import type { DocumentInfo, HeaderFileItem, SyncState } from '../types'

export type FileSwitcherProps = {
  doc: DocumentInfo
  sync: SyncState
  /** 可见项目列表(App 从 03-authentication useProject().projects 注入) */
  projects?: HeaderFileItem[]
  /** LRU id 序(App 从 localStorage['ze.recent.<uid>'] 注入) */
  recentIds?: string[]
  recentMax?: number
  /** 打开下拉瞬间刷新列表 */
  onRefresh?: () => void
  /** 示例场景模板(示例区;行只允许 open/reset,无 rename/delete/duplicate) */
  demoTemplates?: { id: string; name: string; sandbox_generation: number }[]
  /** 当前是否 demo 沙盒态(驱动示例徽记 + 「另存为普通项目」) */
  isDemoSession?: boolean
  /** mod 在操作区末尾追加(SPEC §2.6 重载点 2) */
  append?: ReactNode
  hideRename?: boolean
  hideDelete?: boolean
}

const ROW_ICON: Record<RowAction, string> = {
  rename: 'edit',
  duplicate: 'copy',
  save_as: 'save-as',
  delete: 'trash',
}

export function FileSwitcher(props: FileSwitcherProps): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const openDialog = useHeaderDialog()
  const { open, triggerRef, popoverRef, toggle, close } = usePopover()
  const fileListRef = useScrollAffordance('y')
  const demoListRef = useScrollAffordance('y')

  // 开即拉:列表无 SSE 广播,打开瞬间刷新(SPEC §2.3.5 边界)
  useEffect(() => {
    if (open) props.onRefresh?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const hasDoc = !!props.doc
  const conflict = props.sync.kind === 'conflict'
  const displayName = props.doc?.name ?? t('header.projectPlaceholder')
  const rows = mergeFileList(props.projects ?? [], props.recentIds ?? [], props.recentMax)

  const toDialog = (kind: DialogKind, payload?: Record<string, unknown>) => {
    openDialog(kind, payload)
    close()
  }
  const openRow = (item: HeaderFileItem) => {
    if (item.id !== props.doc?.id) dispatch(CMD_DOCUMENT_OPEN, { doc_id: item.id })
    close()
  }
  const rowAction = (e: MouseEvent, action: RowAction, item: HeaderFileItem) => {
    e.stopPropagation()                      // 不触发行点击(切换打开)
    const target = rowActionTarget(item)
    if (action === 'duplicate') {
      dispatch(CMD_DOCUMENT_DUPLICATE, {
        doc_id: item.id,
        expected_generation: item.generation,
      })
      close()
      return
    }
    toDialog(action === 'save_as' ? 'save_as' : action, { target })
  }

  const ROW_LABEL: Record<RowAction, string> = {
    rename: t('file.rename'),
    duplicate: t('file.duplicate'),
    save_as: t('file.saveAs'),
    delete: t('file.delete'),
  }

  return (
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <button
        ref={triggerRef}
        type="button"
        className="ze-header__file-trigger"
        data-open={open ? 'true' : 'false'}
        data-placeholder={!hasDoc ? 'true' : undefined}
        data-demo={props.isDemoSession ? 'true' : undefined}
        title={props.doc?.data_dir_path ?? t('header.projectHoverEmpty')}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('header.fileSwitcherAria')}
        onClick={toggle}
      >
        {props.isDemoSession && (
          <span className="ze-header__file-demo-badge">{t('header.demoBadge')}</span>
        )}
        <span className="ze-header__file-trigger-name">{displayName}</span>
        <span className="ze-header__menu-caret ze-icon" data-icon="chevron-down" data-size="xs" aria-hidden="true" />
      </button>

      <MenuPopover
        open={open}
        anchor="center"
        wide
        popoverRef={popoverRef}
        ariaLabel={t('header.fileSwitcherAria')}
      >
        {/* 文件列表区:行 = 切换打开;hover 出行内按钮组 */}
        <MenuGroup>
          <div className="ze-header__file-list ze-scroll-fade-y" role="presentation" ref={fileListRef}>
            {rows.length === 0 && (
              <div className="ze-header__acl-title" role="presentation">{t('file.listEmpty')}</div>
            )}
            {rows.map((item) => {
              const current = item.id === props.doc?.id
              return (
                <div
                  key={item.id}
                  className="ze-header__file-row"
                  data-current={current ? 'true' : undefined}
                  role="menuitem"
                  tabIndex={0}
                  aria-label={item.name}
                  onClick={() => openRow(item)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openRow(item) }
                  }}
                >
                  <span className="ze-header__file-row-check" aria-hidden="true">
                    {current && <ZeIcon name="check" size="xs" />}
                  </span>
                  <span className="ze-header__file-row-name">{item.name}</span>
                  <span className="ze-header__file-row-meta">{relativeTime(item.updated_at, t('file.justNow'))}</span>
                  <span className="ze-header__file-row-actions">
                    {rowActions(item, { hideRename: props.hideRename, hideDelete: props.hideDelete })
                      .map((action) => (
                        <button
                          key={action}
                          type="button"
                          className="ze-header__file-row-action"
                          data-variant={action === 'delete' ? 'danger' : undefined}
                          aria-label={`${ROW_LABEL[action]} — ${item.name}`}
                          title={ROW_LABEL[action]}
                          onClick={(e) => rowAction(e, action, item)}
                        >
                          <ZeIcon name={ROW_ICON[action]} size="xs" />
                        </button>
                      ))}
                  </span>
                </div>
              )
            })}
          </div>
        </MenuGroup>

        {/* 示例区:行只允许 open/reset(无 rename/delete/duplicate,避免误改示例) */}
        {(props.demoTemplates?.length ?? 0) > 0 && (
          <MenuGroup>
            <div className="ze-header__file-list ze-scroll-fade-y" role="presentation" ref={demoListRef}>
              <div className="ze-header__acl-title" role="presentation">{t('file.examples')}</div>
              {props.demoTemplates!.map((d) => (
                <div
                  key={d.id}
                  className="ze-header__file-row"
                  role="menuitem"
                  tabIndex={0}
                  aria-label={d.name}
                  onClick={() => { dispatch(CMD_DEMO_OPEN, {
                    id: d.id,
                    expected_generation: d.sandbox_generation,
                  }); close() }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); dispatch(CMD_DEMO_OPEN, {
                      id: d.id,
                      expected_generation: d.sandbox_generation,
                    }); close() }
                  }}
                >
                  <span className="ze-header__file-row-check" aria-hidden="true" />
                  <span className="ze-header__file-row-name">{d.name}</span>
                  <span className="ze-header__file-row-meta">{t('file.openOrReset')}</span>
                </div>
              ))}
            </div>
          </MenuGroup>
        )}

        {/* 操作区:非列表型文件操作 + mod append(重载点 2) */}
        <MenuGroup>
          {props.isDemoSession && (
            <MenuItem
              label={t('file.saveAsNormal')}
              onClick={() => toDialog('save_as', { target: {
                id: props.doc?.id,
                name: props.doc?.name,
                generation: props.doc?.generation,
              } })}
            />
          )}
          <MenuItem label={t('file.new')} shortcut="Ctrl/Cmd+N" onClick={() => toDialog('new')} />
          <MenuItem label={t('file.import')} disabled={!hasDoc} onClick={() => toDialog('import')} />
          <MenuItem label={t('file.export')} disabled={!hasDoc} onClick={() => toDialog('export')} />
          <MenuItem
            label={t('file.snapshot')}
            shortcut="Ctrl/Cmd+S"
            disabled={!hasDoc || conflict}
            onClick={() => toDialog('snapshot')}
          />
          <MenuItem label={t('file.close')} shortcut="Ctrl/Cmd+W" disabled={!hasDoc} onClick={() => toDialog('close')} />
        </MenuGroup>

        {props.append && <MenuGroup>{props.append}</MenuGroup>}
      </MenuPopover>
    </div>
  )
}

/** 行右 meta:相对时间(mono 小字),超 30 天显日期 */
function relativeTime(ts: number, justNow: string): string {
  const ms = ts > 1e12 ? ts : ts * 1000
  const diff = Date.now() - ms
  const MIN = 60_000
  const HOUR = 60 * MIN
  const DAY = 24 * HOUR
  if (diff < MIN) return justNow
  if (diff < HOUR) return `${Math.floor(diff / MIN)}m`
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h`
  if (diff < 30 * DAY) return `${Math.floor(diff / DAY)}d`
  const d = new Date(ms)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
