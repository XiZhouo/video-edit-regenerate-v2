/**
 * displayDialogs — 纯展示类对话框(不 dispatch,只读 config/doc 显示)
 *
 * SPEC: ../../SPEC.md §2.3.7 帮助菜单
 *   - AboutDialog    7.3 关于:skill 名 + 版本信息(有什么显什么,缺的显 —)
 *   - ShortcutsDialog 7.2 快捷键:聚合 HEADER_SHORTCUTS
 *   - ManualDialog   7.1 操作手册:config.manual 有则给链接,无则提示未配置
 *
 * 这三个不依赖后端,M1 即完全可用。
 */

import { type CSSProperties, type ReactNode } from 'react'

import { useT, type I18nKey } from '@ze/core/04-user-facing/05-preferences'
import {
  OperationManualContent,
  resolveOperationManualUrl,
  useOperationManual,
} from '../../lib/operationManual'
import { BaseDialog } from './BaseDialog'
import { HEADER_SHORTCUTS } from '../commands'
import type { HeaderConfig, DocumentInfo } from '../types'

// 视觉单一真相源:字号/墨色走 .ze-text —— 行容器贴 .ze-text 给 fs-sm 基线 + ink-primary,
// KEY 用 data-tone=muted(三级墨)、VAL 用 base + 下面 wordBreak 结构、快捷键 kbd 用 data-mono。
// 这两个 const 只留**结构**(网格/间距/换行)。禁止内联视觉(css_layer_guard 规则 8)。
const ROW: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '120px 1fr',
  gap: 'var(--ze-space-3)',
  padding: 'var(--ze-space-1) 0',
}
const VAL: CSSProperties = { wordBreak: 'break-all' }


// ============================================================
// 7.3 关于
// ============================================================

export type AboutDialogProps = {
  config?: HeaderConfig
  doc: DocumentInfo
  /** mod 可补充的额外字段(业务版本等);SPEC §2.6 about_extras */
  extras?: Array<{ label: string; value: string }>
  onClose: () => void
}

export function AboutDialog({ config, doc, extras, onClose }: AboutDialogProps): ReactNode {
  const t = useT()
  const rows: Array<[string, string]> = [
    ['Skill', config?.skill_name ?? 'Unnamed Skill'],
    [t('dialog.about.currentProject'), doc ? doc.name : '—'],
    [t('dialog.about.schemaVersion'), doc ? doc.schema_version : '—'],
    [t('dialog.about.collabMode'), doc ? doc.collaboration_mode : '—'],
    ...(extras ?? []).map((e) => [e.label, e.value] as [string, string]),
  ]
  return (
    <BaseDialog title={t('dialog.about.title')} size="sm" onClose={onClose}
      footer={<button className="ze-button" data-variant="primary" data-size="sm" onClick={onClose}>{t('common.gotIt')}</button>}>
      <div>
        {rows.map(([k, v], i) => (
          <div key={`${k}-${i}`} className="ze-text" style={ROW}><span className="ze-text" data-tone="muted">{k}</span><span className="ze-text" style={VAL}>{v}</span></div>
        ))}
      </div>
    </BaseDialog>
  )
}


// ============================================================
// 7.2 快捷键
// ============================================================

function formatCombo(combo: string): string {
  return combo
    .split('+')
    .map((k) => (k === 'mod' ? 'Ctrl/Cmd' : k.charAt(0).toUpperCase() + k.slice(1)))
    .join(' + ')
}

// cmd id → i18n key(无点 leaf;resolve 按 '.' 拆,不能用点号做 key)
const SHORTCUT_LABEL_KEY: Record<string, I18nKey> = {
  'document.new': 'dialog.shortcuts.new',
  'document.open': 'dialog.shortcuts.open',
  'document.snapshot': 'dialog.shortcuts.snapshot',
  'document.save_as': 'dialog.shortcuts.saveAs',
  'document.close': 'dialog.shortcuts.close',
  'system.help.manual': 'dialog.shortcuts.manual',
  'system.help.shortcuts': 'dialog.shortcuts.shortcutsPanel',
}

export function ShortcutsDialog({ onClose }: { onClose: () => void }): ReactNode {
  const t = useT()
  const entries = Object.entries(HEADER_SHORTCUTS)
  return (
    <BaseDialog title={t('dialog.shortcuts.title')} size="sm" onClose={onClose}
      footer={<button className="ze-button" data-variant="primary" data-size="sm" onClick={onClose}>{t('common.gotIt')}</button>}>
      <div>
        {entries.map(([combo, cmd]) => (
          <div key={combo} className="ze-text" style={ROW}>
            <kbd className="ze-text" data-mono="true" data-tone="muted">{formatCombo(combo)}</kbd>
            <span className="ze-text" style={VAL}>{SHORTCUT_LABEL_KEY[cmd] ? t(SHORTCUT_LABEL_KEY[cmd]) : cmd}</span>
          </div>
        ))}
      </div>
    </BaseDialog>
  )
}


// ============================================================
// 7.1 操作手册
// ============================================================

export function ManualDialog({ config, onClose }: { config?: HeaderConfig; onClose: () => void }): ReactNode {
  const t = useT()
  const manual = config?.manual
  const manualLink = manual ? resolveOperationManualUrl(manual)?.href : undefined
  // Fetching, redirect/media URL resolution and HTML sanitation all live in
  // operationManual.  Manual surfaces never render downstream HTML directly.
  const operationManual = useOperationManual(config?.manual_html)
  return (
    <BaseDialog title={t('dialog.manual.title')} size="lg" onClose={onClose}
      footer={<button className="ze-button" data-size="sm" onClick={onClose}>{t('common.close')}</button>}>
      {operationManual.fragment ? (
        <OperationManualContent fragment={operationManual.fragment} />
      ) : operationManual.status === 'loading' ? (
        <div className="ze-text" data-tone="muted">{t('common.loading')}</div>
      ) : manualLink ? (
        <div className="ze-text">
          <p className="ze-text" data-role="secondary" style={{ marginTop: 0 }}>{t('dialog.manual.located')}</p>
          <a className="ze-link" data-external="true" href={manualLink} target="_blank" rel="noreferrer">{manualLink}</a>
        </div>
      ) : (
        <div className="ze-callout" data-variant="info">
          <span className="ze-callout__icon ze-icon" data-icon="info" aria-hidden="true" />
          <div>
            <div className="ze-callout__title">{t('dialog.manual.unconfiguredTitle')}</div>
            <div className="ze-callout__body">{t('dialog.manual.unconfiguredBody')}</div>
          </div>
        </div>
      )}
    </BaseDialog>
  )
}
