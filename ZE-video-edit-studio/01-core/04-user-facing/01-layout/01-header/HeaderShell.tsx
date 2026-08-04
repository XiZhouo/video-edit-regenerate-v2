/**
 * HeaderShell — Header 三段容器 + Context Provider
 *
 * SPEC: ./SPEC.md §2.2 三段布局 + §2.7 跟其他叶子的接口
 *
 * 本组件:
 *   1. 提供 .ze-header 三段 grid 容器
 *   2. 用 HeaderDispatchContext.Provider 注入 dispatch(批 4 hook 替换默认 stub)
 *   3. 装 LeftSegment / MiddleSegment / RightSegment
 *   4. import Header CSS(整个 Header 视觉由本文件触发加载)
 *
 * 批 4 hook 数据(doc / sync / user / theme)由 props 注入;未注入时各组件
 * 退化到合理 stub(无 doc / unavailable sync / 匿名 / system 主题)。
 */

import { useCallback, useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { LeftSegment } from './LeftSegment'
import { MiddleSegment } from './MiddleSegment'
import { RightSegment } from './RightSegment'
import { HeaderDispatchContext, type DispatchFn } from './lib/dispatch'
import {
  HeaderDialogContext, DialogHost,
  type DialogKind, type DialogPayload,
} from './dialogs'
import type {
  HeaderProps,
  HeaderFileItem,
  SyncState,
  DocumentInfo,
  CurrentUser,
} from './types'
import type { Theme } from '@ze/core/04-user-facing/05-preferences'
import type { Locale } from '@ze/i18n'

// CSS 入口:整个 Header 视觉由本文件触发加载
// (Stage B 加 build alias 后改成 @css/04-editors/header.css)
import '../../../../03-css-rules/05-compositions/navigation/header/code.css'

// ============================================================
// Props
// ============================================================

export type HeaderShellProps = HeaderProps & {
  /** 真实 dispatch;批 4 hook 接 01-command;未提供则 Context 默认 stub */
  dispatch?: DispatchFn
  /** 当前 doc;批 4 useDocument() 提供;未提供 = 未打开 doc */
  doc?: DocumentInfo
  /** 同步状态;批 4 useSyncState() 提供;未提供 = unavailable */
  sync?: SyncState
  /** 当前用户;批 4 useAuth() 提供;未提供 = 匿名 */
  user?: CurrentUser
  /** 当前主题;PreferencesProvider 的 useTheme() 提供;未提供 = system */
  theme?: Theme
  /** 主题切换回调(Provider 维护状态 + 写 localStorage + 应用 data-theme)*/
  onThemeChange?: (theme: Theme) => void
  /** 当前语言;PreferencesProvider 的 useLocale() 提供;未提供 = zh */
  locale?: Locale
  /** 语言切换回调(Provider 写 localStorage + 应用 <html lang> + 触发重渲染)*/
  onLocaleChange?: (locale: Locale) => void
  /** 当前文档 acl(头像成员面板用;owner 可增删协作者,其余只读)*/
  acl?: { owner?: string; collaborators?: string[] }
  /** 当前登录用户 id(成员面板 owner 判定)*/
  currentUserId?: string
  /** 文件切换器列表(header-v2;App 从 03-authentication useProject().projects 注入)*/
  projects?: HeaderFileItem[]
  /** LRU id 序(App 从 localStorage['ze.recent.<uid>'] 读出注入)*/
  recentIds?: string[]
  /** 文件下拉打开瞬间刷新列表(App 接 useProject().refresh)*/
  onRefreshProjects?: () => void
  /** 示例场景模板(文件切换器示例区;App 从 useProject().demoTemplates 注入)*/
  demoTemplates?: { id: string; name: string; sandbox_generation: number }[]
  /** 当前是否 demo 沙盒态(App 从 doc.config.project_kind 推导;驱动示例徽记 + 另存为普通项目)*/
  isDemoSession?: boolean
  /** 点品牌字标回「我的项目」首页(App 接 useProject().closeProject);给了才把字标渲成可点按钮。 */
  onHome?: () => void
}

// ============================================================
// 组件
// ============================================================

export function HeaderShell(props: HeaderShellProps): ReactNode {
  const { config, overrides } = props
  const t = useT()

  // 产品名=可译 chrome:mod 在 09-i18n 声明 mod.product.name(zh+en)即走 t();
  // 未声明时 translate 回退 key 串本身 → 退到 overrides/config 的 skill_name 常量(向后兼容)。
  // mod overrides 的 fallback 链:mod.product.name(i18n)> overrides > config > core default
  // 完整 default 保证:`<HeaderShell />` 不传任何 props 也能跑(即"模板态")
  const productName = t('mod.product.name')
  const skillName = productName && productName !== 'mod.product.name'
    ? productName
    : (overrides?.skill_name ?? config?.skill_name ?? 'Unnamed Skill')
  const skillIcon = overrides?.skill_icon ?? config?.skill_icon

  // 批 4 stub 数据 fallback
  const doc = props.doc ?? null
  const sync: SyncState = props.sync ?? { kind: 'unavailable' }
  const user = props.user ?? null
  const theme: Theme = props.theme ?? 'system'
  const locale: Locale = props.locale ?? 'zh'

  // 对话框打开状态(菜单项 openDialog → 这里记 kind;DialogHost 渲染)
  const [dialog, setDialog] = useState<{ kind: DialogKind; payload?: DialogPayload } | null>(null)
  // useCallback 稳定引用:避免每次 render 重建 → context value 变 → 菜单重渲染 / BaseDialog 重挂监听
  const openDialog = useCallback((kind: DialogKind, payload?: DialogPayload) => setDialog({ kind, payload }), [])
  const closeDialog = useCallback(() => setDialog(null), [])

  const content = (
    <header
      className="ze-header"
      data-ze-region="header"
      role="banner"
    >
      <LeftSegment
        skillName={skillName}
        skillIcon={skillIcon}
        slot={overrides?.left_slot}
        onHome={props.onHome}
        homeHint={props.onHome ? t('header.home') : undefined}
      />

      <MiddleSegment
        doc={doc}
        sync={sync}
        projects={props.projects}
        recentIds={props.recentIds}
        recentMax={config?.recent_max ?? 10}
        onRefreshProjects={props.onRefreshProjects}
        demoTemplates={props.demoTemplates}
        isDemoSession={props.isDemoSession}
        fileMenuAppend={overrides?.file_menu_append}
        hideRename={overrides?.hide?.includes('rename')}
        hideDelete={overrides?.hide?.includes('delete')}
        slot={overrides?.middle_slot}
      />

      <RightSegment
        doc={doc}
        sync={sync}
        user={user}
        theme={theme}
        onThemeChange={props.onThemeChange}
        locale={locale}
        onLocaleChange={props.onLocaleChange}
        appendButtons={overrides?.right_append}
        settingsMenuAppend={overrides?.settings_menu_append}
        helpMenuAppend={overrides?.help_menu_append}
        hideFeedback={overrides?.hide?.includes('feedback')}
        acl={props.acl}
        currentUserId={props.currentUserId}
      />
    </header>
  )

  // dialog context(菜单项打开对话框)+ DialogHost 一起包进来
  const withDialogs = (
    <HeaderDialogContext.Provider value={openDialog}>
      {content}
      <DialogHost
        active={dialog}
        onClose={closeDialog}
        doc={doc}
        config={config}
        sync={sync}
        overrides={overrides?.dialogs}
      />
    </HeaderDialogContext.Provider>
  )

  // dispatch 注入(批 4 hook 提供)— 否则走 Context 默认 stub(console.warn)。
  // DialogHost 在 dispatch provider 之内,故对话框确认时能拿到真实 dispatch。
  if (props.dispatch) {
    return (
      <HeaderDispatchContext.Provider value={props.dispatch}>
        {withDialogs}
      </HeaderDispatchContext.Provider>
    )
  }

  return withDialogs
}
