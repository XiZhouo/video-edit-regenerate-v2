/**
 * HelpMenu — 帮助菜单 4 子项
 *
 * SPEC: ./SPEC.md §2.3.7
 *
 * 4 子项:
 *   7.1 操作手册 → CMD_SYSTEM_HELP_MANUAL
 *   7.2 快捷键 → CMD_SYSTEM_HELP_SHORTCUTS(同时绑全局 ? 单键)
 *   7.3 关于 → CMD_SYSTEM_HELP_ABOUT
 *   7.4 反馈 → CMD_SYSTEM_HELP_FEEDBACK(可由 mod hide)
 */

import { type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'
import { useHeaderDispatch } from '../lib/dispatch'
import { useHeaderDialog } from '../dialogs'
import { usePopover } from '../lib/usePopover'
import { MenuPopover } from './MenuPopover'
import { MenuItem, MenuGroup } from './MenuItem'
import { CMD_SYSTEM_HELP_FEEDBACK } from '../commands'

export type HelpMenuProps = {
  /** 是否隐藏反馈(SPEC §2.6 重载点 4 白名单) */
  hideFeedback?: boolean
  /** mod 追加(SPEC §2.6 重载点 2) */
  append?: ReactNode
}

export function HelpMenu(props: HelpMenuProps): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const openDialog = useHeaderDialog()
  const { open, triggerRef, popoverRef, toggle, close } = usePopover()

  // 操作手册 / 快捷键 / 关于 → 打开对话框;反馈 → dispatch(开 URL)
  const toDialog = (kind: Parameters<typeof openDialog>[0]) => { openDialog(kind); close() }
  const feedback = () => { dispatch(CMD_SYSTEM_HELP_FEEDBACK); close() }

  return (
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <button
        ref={triggerRef}
        type="button"
        className="ze-header__menu-trigger"
        data-open={open ? 'true' : 'false'}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('header.helpAria')}
        title={t('header.helpAria')}
        onClick={toggle}
      >
        <ZeIcon name="help" />
      </button>

      <MenuPopover
        open={open}
        anchor="right"
        popoverRef={popoverRef}
        ariaLabel={t('header.helpMenuAria')}
      >
        <MenuGroup>
          <MenuItem
            label={t('help.manual')}
            onClick={() => toDialog('manual')}
          />
          <MenuItem
            label={t('help.shortcuts')}
            shortcut="?"
            onClick={() => toDialog('shortcuts')}
          />
          <MenuItem
            label={t('help.about')}
            onClick={() => toDialog('about')}
          />
          {!props.hideFeedback && (
            <MenuItem
              label={t('help.feedback')}
              onClick={feedback}
            />
          )}
        </MenuGroup>

        {props.append && <MenuGroup>{props.append}</MenuGroup>}
      </MenuPopover>
    </div>
  )
}
