/**
 * confirmDialogs — 确认 / 销毁类对话框
 *
 * SPEC: ../../SPEC.md §2.3.5.6 关闭 / §2.3.5.9 删除 / §2.3.9 登录
 *   - DeleteDialog 5.9 红线:显示项目名 + 路径,**必须手打全名才能启用删除按钮**
 *     (SPEC §2.6 delete_require_text_confirmation 永远 true,mod 不可关)
 *   - CloseConfirmDialog 5.6:仅当 syncing/delayed/conflict 才确认;in-sync 不弹(由触发方判断)
 *   - LoginDialog 9:M1 stub
 *
 * dispatch 经 useHeaderDispatch()(HeaderShell 注入;模板态走 stub)。
 */

import { useState, type CSSProperties, type ReactNode } from 'react'

import { useT, Trans } from '@ze/core/04-user-facing/05-preferences'
import { BaseDialog } from './BaseDialog'
import { useHeaderDispatch } from '../lib/dispatch'
import { CMD_DOCUMENT_DELETE, CMD_DOCUMENT_CLOSE, CMD_AUTH_LOGIN } from '../commands'
import type { DocumentInfo, SyncState } from '../types'

// 视觉单一真相源:文字色/字号走 .ze-text(标签=data-role=secondary+size=xs、值=base、mono 值=data-mono);
// 这两个 const 只留**结构**(布局/网格/间距),不含视觉。禁止内联视觉(css_layer_guard 规则 8)。
const FIELD: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 'var(--ze-space-1)', marginTop: 'var(--ze-space-3)' }
const KV: CSSProperties = { display: 'grid', gridTemplateColumns: '88px 1fr', gap: 'var(--ze-space-2)', padding: 'var(--ze-space-0-5) 0' }


// ============================================================
// 5.9 删除项目(红线:手打全名才启用删除)
// ============================================================
export function DeleteDialog({ doc, onClose }: { doc: DocumentInfo; onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [typed, setTyped] = useState('')
  const targetName = doc?.name ?? ''
  // 红线:输入必须精确等于项目名才解禁
  const armed = typed.length > 0 && typed === targetName
  const ok = () => {
    if (!armed) return
    dispatch(CMD_DOCUMENT_DELETE, {
      id: doc?.id,
      confirm_name: typed,
      expected_generation: doc?.generation,
    })
    onClose()
  }
  return (
    <BaseDialog title={t('dialog.delete.title')} size="sm" onClose={onClose}
      footer={
        <>
          <button className="ze-button" data-size="sm" onClick={onClose}>{t('common.cancel')}</button>
          <button className="ze-button" data-variant="danger" data-size="sm" disabled={!armed} onClick={ok}>{t('dialog.delete.ok')}</button>
        </>
      }>
      <div className="ze-callout" data-variant="danger">
        <span className="ze-callout__icon ze-icon" data-icon="warn" aria-hidden="true" />
        <div>
          <div className="ze-callout__title">{t('dialog.delete.warnTitle')}</div>
          <div className="ze-callout__body">{t('dialog.delete.warnBody')}</div>
        </div>
      </div>
      <div style={{ marginTop: 'var(--ze-space-3)' }}>
        <div className="ze-text" style={KV}><span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.delete.nameLabel')}</span><span>{targetName || '—'}</span></div>
        <div className="ze-text" style={KV}><span className="ze-text" data-role="secondary" data-size="xs">doc_id</span><span className="ze-text" data-mono="true">{doc?.id ?? '—'}</span></div>
      </div>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs"><Trans k="dialog.delete.confirm" params={{ name: targetName }} /></span>
        <input className="ze-input" value={typed} aria-invalid={typed.length > 0 && !armed ? 'true' : undefined}
          placeholder={targetName}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') ok() }} />
      </label>
    </BaseDialog>
  )
}


// ============================================================
// 5.6 关闭(条件确认:同步未完成 / 有冲突)
// ============================================================
export function CloseConfirmDialog({ sync, onClose }: { sync: SyncState; onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const conflict = sync.kind === 'conflict'
  const close = (discard: boolean) => { dispatch(CMD_DOCUMENT_CLOSE, { discard }); onClose() }
  return (
    <BaseDialog title={t('dialog.close.title')} size="sm" onClose={onClose}
      footer={
        <>
          <button className="ze-button" data-size="sm" onClick={onClose}>{t('common.cancel')}</button>
          <button className="ze-button" data-variant="danger" data-size="sm" onClick={() => close(true)}>{t('dialog.close.discard')}</button>
          <button className="ze-button" data-variant="primary" data-size="sm" onClick={() => close(false)}>{t('dialog.close.waitSync')}</button>
        </>
      }>
      <div className="ze-callout" data-variant={conflict ? 'danger' : 'warn'}>
        <span className="ze-callout__icon ze-icon" data-icon="warn" aria-hidden="true" />
        <div>
          <div className="ze-callout__title">{conflict ? t('dialog.close.conflictTitle') : t('dialog.close.dirtyTitle')}</div>
          <div className="ze-callout__body">
            {conflict ? t('dialog.close.conflictBody') : t('dialog.close.dirtyBody')}
          </div>
        </div>
      </div>
    </BaseDialog>
  )
}


// ============================================================
// 9. 登录(M1 stub)
// ============================================================
export function LoginDialog({ onClose }: { onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const ok = () => { dispatch(CMD_AUTH_LOGIN, {}); onClose() }
  return (
    <BaseDialog title={t('dialog.login.title')} size="sm" onClose={onClose}
      footer={
        <>
          <button className="ze-button" data-size="sm" onClick={onClose}>{t('common.cancel')}</button>
          <button className="ze-button" data-variant="primary" data-size="sm" onClick={ok}>{t('dialog.login.ok')}</button>
        </>
      }>
      <div className="ze-callout" data-variant="info">
        <span className="ze-callout__icon ze-icon" data-icon="info" aria-hidden="true" />
        <div>
          <div className="ze-callout__title">{t('dialog.login.stubTitle')}</div>
          <div className="ze-callout__body">{t('dialog.login.stubBody')}</div>
        </div>
      </div>
    </BaseDialog>
  )
}
