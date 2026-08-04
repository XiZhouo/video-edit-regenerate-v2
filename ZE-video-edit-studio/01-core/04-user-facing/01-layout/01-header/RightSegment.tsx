/**
 * RightSegment — Header 右段
 *
 * SPEC: ./SPEC.md §2.3.6 设置菜单 + §2.3.7 帮助菜单 + §2.3.9 头像
 *
 * 渲染顺序(左→右,header-v2):
 *   1. SettingsMenu(icon 令牌 settings)— 主题 / 语言 / 数据目录 / 协作 / 配置
 *   2. HelpMenu(icon 令牌 help)
 *   3. mod appendButtons(挂在用户头像前)
 *   4. UserAvatar(含成员/共享面板 —— 原 ShareButton 自 1.31.0 并入头像菜单)
 *
 * 注:主题切换已收进设置菜单(6.1),不再有独立 ◐ 控件;theme/onThemeChange 仍下传给 SettingsMenu。
 */

import { type ReactNode } from 'react'

import { SettingsMenu } from './menus/SettingsMenu'
import { HelpMenu } from './menus/HelpMenu'
import { UserAvatar } from './controls/UserAvatar'
import type { ShareAcl } from './controls/AclPanel'
import type { DocumentInfo, SyncState, CurrentUser } from './types'
import type { Theme } from '@ze/core/04-user-facing/05-preferences'
import type { Locale } from '@ze/i18n'

export type RightSegmentProps = {
  doc: DocumentInfo
  sync: SyncState
  user: CurrentUser
  theme: Theme
  onThemeChange?: (t: Theme) => void
  locale: Locale
  onLocaleChange?: (l: Locale) => void
  appendButtons?: ReactNode
  settingsMenuAppend?: ReactNode
  helpMenuAppend?: ReactNode
  hideFeedback?: boolean
  /** 当前文档 acl(共享按钮用;owner 才显示)*/
  acl?: ShareAcl
  /** 当前登录用户 id(== acl.owner 时显示共享按钮)*/
  currentUserId?: string
}

export function RightSegment(props: RightSegmentProps): ReactNode {
  return (
    <div className="ze-header__right" data-ze-segment="right">
      <SettingsMenu
        theme={props.theme}
        onThemeChange={props.onThemeChange}
        locale={props.locale}
        onLocaleChange={props.onLocaleChange}
        doc={props.doc}
        sync={props.sync}
        append={props.settingsMenuAppend}
      />

      <HelpMenu
        hideFeedback={props.hideFeedback}
        append={props.helpMenuAppend}
      />

      {props.appendButtons}

      {/* header-v2:「共享」并入头像菜单(成员面板);右段只剩 设置/帮助/头像 */}
      <UserAvatar
        user={props.user}
        acl={props.acl}
        currentUserId={props.currentUserId}
        documentVersion={props.doc?._v}
      />
    </div>
  )
}
