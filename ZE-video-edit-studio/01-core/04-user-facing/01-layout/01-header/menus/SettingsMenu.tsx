/**
 * SettingsMenu — 设置菜单 4 子项
 *
 * SPEC: ./SPEC.md §2.3.6
 *
 * 4 子项:
 *   6.1 主题 — 3 选项(亮 / 暗 / 跟随系统)
 *   6.2 语言 — 中 / 英
 *   6.3 数据目录 — 只读路径
 *   6.4 协作状态 — 只读详情(模式 / 协作者数 / 同步延迟)
 *
 * 文案走 i18n:`useT()`(@ze/core/.../05-preferences),key 见 05-i18n/zh.ts 的 settings.* / theme.*。
 */

import { useState, type ReactNode, useEffect } from 'react'

import { usePopover } from '../lib/usePopover'
import { MenuPopover } from './MenuPopover'
import { MenuItem, MenuGroup } from './MenuItem'
import { ConfigDialog } from './ConfigDialog'
import { CopilotAccountDialog } from '../../copilot'
import {
  notifyCredentialsChanged,
  OPEN_CREDENTIALS_EVENT,
} from '../../../03-authentication/aiRoutes'
import { isAiSettingsTarget, parseAiSettingsHash, type AiSettingsTarget }
  from '../../../03-authentication/aiSettingsLink'   // 统一「AI 账号」页(Codex/Claude/组织网关 key)
import { useT, type Theme } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'
import type { DocumentInfo, SyncState } from '../types'
import { LOCALE_CODES, LOCALE_META, type Locale } from '@ze/i18n'

export type SettingsMenuProps = {
  theme: Theme
  onThemeChange?: (t: Theme) => void
  locale?: Locale
  onLocaleChange?: (l: Locale) => void
  doc: DocumentInfo
  sync: SyncState
  append?: ReactNode
}

export function SettingsMenu(props: SettingsMenuProps): ReactNode {
  const t = useT()
  const { open, triggerRef, popoverRef, toggle, close } = usePopover()
  const [cfgOpen, setCfgOpen] = useState(false)
  const [credOpen, setCredOpen] = useState(false)
  const [credTarget, setCredTarget] = useState<AiSettingsTarget | undefined>()
  // 「没绑就先说」一键修复出口(3.56):AiRouteGate 等入口派发该事件直达账号弹窗。
  // 事件带落点(§5.1 deep-link):派发方已经知道该去哪一页,这里只负责转达。
  useEffect(() => {
    const open = (ev: Event) => {
      const detail = (ev as CustomEvent<unknown>).detail
      setCredTarget(isAiSettingsTarget(detail) ? detail : undefined)
      setCredOpen(true)
    }
    window.addEventListener(OPEN_CREDENTIALS_EVENT, open)
    return () => window.removeEventListener(OPEN_CREDENTIALS_EVENT, open)
  }, [])
  // 跨工具 deep-link:别的 ZE 工具给出的链接落到本工具时,直接开到指定页。
  // 事件进不来别的进程,hash 可以 —— 这正是 §5.1「跨工具稳定」的那一半。
  useEffect(() => {
    const fromHash = () => {
      const parsed = parseAiSettingsHash(window.location.hash)
      if (!parsed) return          // 不是本路由/页名不认识:不猜默认页,交给页面原有行为
      setCredTarget(parsed)
      setCredOpen(true)
    }
    fromHash()
    window.addEventListener('hashchange', fromHash)
    return () => window.removeEventListener('hashchange', fromHash)
  }, [])
  const locale: Locale = props.locale ?? 'zh'

  // 偏好直接走 Provider 回调(本机 localStorage + 即时应用),不再发后端命令。
  const setTheme = (th: Theme) => {
    props.onThemeChange?.(th)
    close()
  }

  const setLocale = (l: Locale) => {
    props.onLocaleChange?.(l)
    close()
  }

  return (
    <>
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <button
        ref={triggerRef}
        type="button"
        className="ze-header__menu-trigger"
        data-open={open ? 'true' : 'false'}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('settings.title')}
        title={t('settings.title')}
        onClick={toggle}
      >
        <ZeIcon name="settings" />
      </button>

      <MenuPopover
        open={open}
        anchor="right"
        popoverRef={popoverRef}
        ariaLabel={t('settings.title')}
      >
        <MenuGroup>
          <SectionHeader text={t('settings.theme')} />
          <RadioMenuItem label={t('theme.opt.light')} active={props.theme === 'light'} onClick={() => setTheme('light')} />
          <RadioMenuItem label={t('theme.opt.dark')} active={props.theme === 'dark'} onClick={() => setTheme('dark')} />
          <RadioMenuItem label={t('theme.opt.system')} active={props.theme === 'system'} onClick={() => setTheme('system')} />
        </MenuGroup>

        <MenuGroup>
          <SectionHeader text={t('settings.language')} />
          {LOCALE_CODES.map((code) => (
            <RadioMenuItem
              key={code}
              label={LOCALE_META[code].label}
              active={locale === code}
              onClick={() => setLocale(code)}
            />
          ))}
        </MenuGroup>

        <MenuGroup>
          <DataDirInfoItem doc={props.doc} />
          <CollabStatusItem sync={props.sync} />
        </MenuGroup>

        <MenuGroup>
          <MenuItem label={t('settings.credentials')} onClick={() => { setCredOpen(true); close() }} />
          <MenuItem label={t('settings.config')} onClick={() => { setCfgOpen(true); close() }} />
        </MenuGroup>

        {props.append && <MenuGroup>{props.append}</MenuGroup>}
      </MenuPopover>
    </div>
    <CopilotAccountDialog
      open={credOpen}
      target={credTarget}
      onClose={() => setCredOpen(false)}
      onChanged={notifyCredentialsChanged}
    />
    <ConfigDialog open={cfgOpen} onClose={() => setCfgOpen(false)} />
    </>
  )
}

// ============================================================
// 子组件
// ============================================================

function SectionHeader(props: { text: string }): ReactNode {
  return (
    <div className="ze-menu-section-header">
      {props.text}
    </div>
  )
}

function RadioMenuItem(props: { label: string; active: boolean; onClick: () => void }): ReactNode {
  // 行首固定图标位:选中 check,未选中留空 16px 位保对齐
  return (
    <MenuItem
      label={
        <>
          {props.active
            ? <ZeIcon name="check" size="xs" />
            : <span className="ze-icon" data-size="xs" aria-hidden="true" />}
          {' '}
          {props.label}
        </>
      }
      onClick={props.onClick}
    />
  )
}

function DataDirInfoItem(props: { doc: DocumentInfo }): ReactNode {
  const t = useT()
  const path = props.doc?.data_dir_path ?? t('settings.noProject')

  return (
    <div className="ze-header__menu-info-item" data-disabled="true" role="presentation">
      <div className="ze-header__menu-info-label">{t('settings.dataDir')}</div>
      <div className="ze-header__menu-info-value" data-monospace="true">
        {path}
      </div>
    </div>
  )
}

function CollabStatusItem(props: { sync: SyncState }): ReactNode {
  const t = useT()
  if (props.sync.kind === 'unavailable') {
    return (
      <div className="ze-header__menu-info-item" data-disabled="true" role="presentation">
        <div className="ze-header__menu-info-label">{t('settings.collabStatus')}</div>
        <div className="ze-header__menu-info-value">{t('sync.unavailable')}</div>
        <div className="ze-header__menu-info-meta">
          sync: <code>{props.sync.kind}</code>
        </div>
      </div>
    )
  }
  // §2.3.6.4 协作状态:模式固定 + 在线协作者 + 同步延迟
  const collaborators = 0  // 批 4 替换
  const delay = props.sync.kind === 'delayed' ? props.sync.delay_ms : 0

  return (
    <div className="ze-header__menu-info-item" data-disabled="true" role="presentation">
      <div className="ze-header__menu-info-label">{t('settings.collabStatus')}</div>
      <div className="ze-header__menu-info-value">
        {t('settings.collabDetail', { count: collaborators, delay })}
      </div>
      <div className="ze-header__menu-info-meta">
        sync: <code>{props.sync.kind}</code>
      </div>
    </div>
  )
}
