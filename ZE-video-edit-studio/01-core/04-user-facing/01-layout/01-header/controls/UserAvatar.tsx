/**
 * UserAvatar — § 2.3.9 用户头像 + 登录下拉
 *
 * 状态:
 *   - 未登录:匿名圆形头像 + click → CMD_AUTH_LOGIN
 *   - 已登录:头像 + click 展开下拉(用户信息 / 切换账号 / 登出)
 *
 * M1:仅匿名头像 + 登录 placeholder;实际登录走 03-authentication(M2/M3)
 */

import { useState, type CSSProperties, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useAuth } from '../../../03-authentication/AuthContext'
import { apiRequestEditPermission } from '../../../03-authentication/authApi'
import { ZeIcon } from '../../lib/zeIcon'
import { useHeaderDispatch } from '../lib/dispatch'
import { usePopover } from '../lib/usePopover'
import { MenuPopover } from '../menus/MenuPopover'
import { MenuItem, MenuGroup } from '../menus/MenuItem'
import { AclPanel, type ShareAcl } from './AclPanel'
import {
  CMD_AUTH_LOGIN,
  CMD_AUTH_LOGOUT,
  CMD_AUTH_SWITCH,
} from '../commands'
import type { CurrentUser } from '../types'

export type UserAvatarProps = {
  user: CurrentUser
  /** 当前文档 acl(header-v2:成员/共享面板并入头像菜单;无 doc 时不渲染面板)*/
  acl?: ShareAcl
  currentUserId?: string
  /** 当前已渲染 Document version；ACL 写入必须用它做 CAS。 */
  documentVersion?: number
}

export function UserAvatar(props: UserAvatarProps): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const { refreshUser } = useAuth()
  const { open, triggerRef, popoverRef, toggle, close } = usePopover()
  const [requesting, setRequesting] = useState(false)
  const [requestNotice, setRequestNotice] = useState('')

  // 未登录:点击直接 dispatch 登录
  if (!props.user) {
    return (
      <button
        type="button"
        className="ze-header__avatar"
        data-state="anonymous"
        aria-label={t('user.loginAria')}
        title={t('user.loginAria')}
        onClick={() => dispatch(CMD_AUTH_LOGIN)}
      >
        <ZeIcon name="user" />
      </button>
    )
  }

  const user = props.user
  const permission = user.is_super_admin ? 'edit' : (user.permission ?? 'read')
  const canRequestEdit = permission === 'read'

  const requestEdit = async () => {
    if (requesting) return
    setRequesting(true)
    setRequestNotice('')
    try {
      const res = await apiRequestEditPermission('user-menu')
      if (res.status === 'already_granted') await refreshUser()
      setRequestNotice(res.status === 'already_granted'
        ? t('auth.permission.alreadyGranted')
        : t('auth.permission.requestSent'))
    } catch (e) {
      setRequestNotice((e as Error).message || t('auth.permission.requestFailed'))
    } finally {
      setRequesting(false)
    }
  }

  return (
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <button
        ref={triggerRef}
        type="button"
        className="ze-header__avatar"
        data-state="signed-in"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={user.name}
        title={user.name}
        onClick={toggle}
        // 视觉单一真相源:头像底图视觉(background-image/size/position)在 header-shell.css
        // 的 .ze-header__avatar[data-state=signed-in];此处只经 CSS var 传运行时 URL(非内联视觉)。
        style={user.avatar_url ? ({ '--ze-avatar-url': `url(${user.avatar_url})` } as CSSProperties) : undefined}
      >
        {!user.avatar_url && (
          <span aria-hidden="true">
            {(user.name.charAt(0) || '?').toUpperCase()}
          </span>
        )}
      </button>

      <MenuPopover
        open={open}
        anchor="right"
        wide={!!props.acl}
        popoverRef={popoverRef}
        ariaLabel={t('header.userMenuAria')}
      >
        <MenuGroup>
          <div
            className="ze-header__menu-info-item"
            data-disabled="true"
            role="presentation"
          >
            <div className="ze-header__menu-info-name">{user.name}</div>
            {user.email && (
              <div className="ze-header__menu-info-meta">{user.email}</div>
            )}
            <div className="ze-header__menu-info-meta">
              {t(`auth.permission.${permission}`)}
            </div>
          </div>
        </MenuGroup>

        {/* 成员/共享面板(header-v2):有 doc(acl 存在)才渲染;owner 可增删,其余只读 */}
        {props.acl && props.currentUserId && (
          <MenuGroup>
            <AclPanel
              acl={props.acl}
              currentUserId={props.currentUserId}
              documentVersion={props.documentVersion ?? 0}
            />
          </MenuGroup>
        )}

        {canRequestEdit && (
          <MenuGroup>
            {requestNotice && (
              <div className="ze-header__menu-info-item" data-disabled="true" role="presentation">
                <div className="ze-header__menu-info-meta">{requestNotice}</div>
              </div>
            )}
            <MenuItem
              label={requesting ? t('auth.busy') : t('auth.permission.requestEdit')}
              disabled={requesting}
              onClick={() => { void requestEdit() }}
            />
          </MenuGroup>
        )}

        <MenuGroup>
          <MenuItem
            label={t('user.switch')}
            onClick={() => {
              dispatch(CMD_AUTH_SWITCH)
              close()
            }}
          />
          <MenuItem
            label={t('user.logout')}
            variant="danger"
            onClick={() => {
              dispatch(CMD_AUTH_LOGOUT)
              close()
            }}
          />
        </MenuGroup>
      </MenuPopover>
    </div>
  )
}
