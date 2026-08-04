/**
 * LocalLinkSettings — Header 设置区「本地文件」面板(F8 状态 + F9 降级)。
 *
 * 纯展示:所有句柄/镜像逻辑在 useLocalLink(挂在常驻的 Workspace,菜单关闭也不卸载,
 * 保证 F4 自动镜像不中断)。本组件只渲染状态并回调动作。
 *
 * 文案走 05-i18n 的 localLink 命名空间(translate(props.locale, ...));`c` 保持原形状,
 * 渲染逻辑不变。
 */

import { useRef, type ReactNode } from 'react'

import { translate } from '@ze/i18n'

import type { LocalLinkActions, LocalLinkState } from './lib/useLocalLink'

type Locale = 'zh' | 'en'

export type LocalLinkSettingsProps = LocalLinkState & LocalLinkActions & {
  locale: Locale
  disabled: boolean
  onExport: () => void
  onImportFile: (file: File) => void
}

export function LocalLinkSettings(props: LocalLinkSettingsProps): ReactNode {
  const tr = (k: string, p?: Record<string, string | number>) =>
    translate(props.locale, `localLink.${k}`, p)
  const c = {
    title: tr('title'),
    export: tr('export'),
    importFile: tr('importFile'),
    link: tr('link'),
    backupNow: tr('backupNow'),
    importFolder: tr('importFolder'),
    unlink: tr('unlink'),
    reconnect: tr('reconnect'),
    neverBackedUp: tr('neverBackedUp'),
    busy: tr('busy'),
    permissionNeeded: tr('permissionNeeded'),
    unsupported: tr('unsupported'),
    failed: tr('failed'),
    linkedTo: (name: string) => tr('linkedTo', { name }),
    lastBackup: (when: string) => tr('lastBackup', { when }),
  }
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const pickFile = () => fileInputRef.current?.click()
  const onFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (file) props.onImportFile(file)
  }

  return (
    <div data-ze-region="local-link">
      <div className="ze-text" data-size="xs" data-tone="secondary" style={{ padding: '4px 8px' }}>
        {c.title}
      </div>

      {/* F8:关联状态 */}
      {props.status === 'linked' && props.dirName ? (
        <div className="ze-text" data-size="sm" style={{ padding: '2px 8px' }}>
          {c.linkedTo(props.dirName)}
        </div>
      ) : null}
      {props.status === 'linked' ? (
        <div className="ze-text" data-size="xs" data-tone="secondary" style={{ padding: '2px 8px' }}>
          {props.busy
            ? c.busy
            : props.lastBackupAt
              ? c.lastBackup(new Date(props.lastBackupAt).toLocaleString())
              : c.neverBackedUp}
        </div>
      ) : null}
      {props.status === 'needs-permission' ? (
        <div className="ze-text" data-size="xs" data-tone="warning" style={{ padding: '2px 8px' }}>
          {c.permissionNeeded}
        </div>
      ) : null}
      {props.status === 'unsupported' ? (
        <div className="ze-text" data-size="xs" data-tone="secondary" style={{ padding: '2px 8px' }}>
          {c.unsupported}
        </div>
      ) : null}
      {props.error ? (
        <div className="ze-text" data-size="xs" data-tone="danger" style={{ padding: '2px 8px' }}>
          {c.failed}
        </div>
      ) : null}

      {/* F9:F1/F2 导出/导入始终可用(降级态下这是唯一入口)。 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '4px 8px' }}>
        <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                disabled={props.disabled} onClick={props.onExport}>
          {c.export}
        </button>
        <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                disabled={props.disabled} onClick={pickFile}>
          {c.importFile}
        </button>

        {/* F3/F5/F6/F7:目录关联能力(仅支持时出现)。 */}
        {props.status === 'idle' ? (
          <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                  disabled={props.disabled} onClick={props.link}>
            {c.link}
          </button>
        ) : null}
        {props.status === 'needs-permission' ? (
          <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                  onClick={props.reconnect}>
            {c.reconnect}
          </button>
        ) : null}
        {props.status === 'linked' ? (
          <>
            <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                    disabled={props.busy} onClick={props.backupNow}>
              {c.backupNow}
            </button>
            <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                    disabled={props.disabled} onClick={props.importFolder}>
              {c.importFolder}
            </button>
          </>
        ) : null}
        {props.status === 'linked' || props.status === 'needs-permission' ? (
          <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                  onClick={props.unlink}>
            {c.unlink}
          </button>
        ) : null}
      </div>

      <input ref={fileInputRef} type="file" accept=".ze-pack,.zip,.json"
             style={{ display: 'none' }} onChange={onFileChange} />
    </div>
  )
}
