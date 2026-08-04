/**
 * PushMessageDialog — 框架自带「推送指定飞书账号」撰写弹窗(notify.push 的输入收集端)
 *
 * 由带 `dialog:'notify.push'` 的 FormAction 点击触发(dialogRegistry 注册)。收件人优先搜索
 * ZE 用户,兼容 open_id / ze.notify.recipients 名册 key + 消息正文 → dispatch('notify.push')。
 * 这是「命令需用户输入」的通用接缝的首个落地:复用 Header 的 BaseDialog 壳 + L2 .ze-input/.ze-textarea
 * /.ze-button 原子;反馈内联(发送中/已发送/失败),不引 toast。文案全走 t()(i18n 门禁)。
 */

import { useState, type CSSProperties, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { BaseDialog } from '@ze/core/04-user-facing/01-layout/01-header/dialogs/BaseDialog'
import { UserPicker } from '@ze/core/04-user-facing/03-authentication'
import type { DialogProps } from './dialogRegistry'

const FIELD: CSSProperties = {
  display: 'flex', flexDirection: 'column', gap: 'var(--ze-space-1)', marginBottom: 'var(--ze-space-3)',
}

export function PushMessageDialog({ dispatch, onClose, presetRecipient, presetMessage }: DialogProps): ReactNode {
  const t = useT()
  const [recipient, setRecipient] = useState(presetRecipient ?? '')
  const [message, setMessage] = useState(presetMessage ?? '')
  const [status, setStatus] = useState<'idle' | 'sending' | 'sent' | 'failed'>('idle')
  const [error, setError] = useState('')

  const canSend = recipient.trim() !== '' && message.trim() !== '' && status !== 'sending'

  const send = async (): Promise<void> => {
    if (!canSend) return
    setStatus('sending')
    setError('')
    try {
      await Promise.resolve(dispatch?.('notify.push', { recipient: recipient.trim(), message: message.trim() }))
      setStatus('sent')
    } catch (e) {
      setStatus('failed')
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const footer: ReactNode = status === 'sent'
    ? <button className="ze-button" data-variant="primary" data-size="sm" onClick={onClose}>{t('common.close')}</button>
    : (
      <>
        <button className="ze-button" data-size="sm" onClick={onClose}>{t('common.cancel')}</button>
        <button className="ze-button" data-variant="primary" data-size="sm" disabled={!canSend} onClick={() => { void send() }}>
          {status === 'sending' ? t('notify.dialog.sending') : t('notify.dialog.send')}
        </button>
      </>
    )

  return (
    <BaseDialog title={t('notify.dialog.title')} size="sm" onClose={onClose} footer={footer}>
      {status === 'sent' ? (
        <p className="ze-text" data-size="sm">{t('notify.dialog.sent')}</p>
      ) : (
        <>
          <label style={FIELD}>
            <span className="ze-text" data-role="secondary" data-size="xs">{t('notify.dialog.recipientLabel')}</span>
            <UserPicker
              value={recipient}
              onChange={setRecipient}
              channel="feishu"
              allowRaw
              placeholder={t('notify.dialog.recipientPlaceholder')}
            />
          </label>
          <label style={FIELD}>
            <span className="ze-text" data-role="secondary" data-size="xs">{t('notify.dialog.messageLabel')}</span>
            <textarea
              className="ze-textarea"
              value={message}
              rows={4}
              onChange={(e) => setMessage(e.target.value)}
            />
          </label>
          {status === 'failed' && (
            <p className="ze-text" data-role="secondary" data-size="xs">{t('notify.dialog.failed')}: {error}</p>
          )}
        </>
      )}
    </BaseDialog>
  )
}
