/**
 * ConfirmDialog — 通用「是否确认」样式化弹窗(替代裸 window.confirm,统一设计语言 + 可主题化 + 不阻塞线程)。
 *
 * 受控组件:调用方持有「待确认动作」状态并渲染本弹窗;确认 → onConfirm(执行动作),取消/关闭 → onCancel。
 * 复用 Header 的 BaseDialog 壳 + L2 .ze-button 原子;文案走 t()。与 dialogRegistry 的输入弹窗
 * (notify.push 等收集参数)分工清晰:本弹窗只做 yes/no 守卫,不收输入。
 * 默认焦点落在「取消」(BaseDialog 取首个 button,即 footer 第一个=取消),破坏性操作不会被回车误触发。
 */

import { type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { BaseDialog } from '@ze/core/04-user-facing/01-layout/01-header/dialogs/BaseDialog'

export function ConfirmDialog({
  message, confirmLabel, variant, onConfirm, onCancel,
}: {
  message: string
  confirmLabel?: string
  /** 确认钮变体:破坏性操作(删除)传 'danger';默认 'primary'。 */
  variant?: 'primary' | 'danger'
  onConfirm: () => void
  onCancel: () => void
}): ReactNode {
  const t = useT()
  const footer: ReactNode = (
    <>
      <button type="button" className="ze-button" data-size="sm" onClick={onCancel}>{t('common.cancel')}</button>
      <button type="button" className="ze-button" data-variant={variant ?? 'primary'} data-size="sm" onClick={onConfirm}>
        {confirmLabel ?? t('common.confirm')}
      </button>
    </>
  )
  return (
    <BaseDialog title={t('common.confirmTitle')} size="sm" onClose={onCancel} footer={footer}>
      <p className="ze-text" data-size="sm">{message}</p>
    </BaseDialog>
  )
}
