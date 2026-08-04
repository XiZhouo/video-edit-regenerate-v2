/**
 * BaseDialog — 所有 Header 对话框的共享模态壳
 *
 * 视觉: 用 L2 的 .ze-dialog / __backdrop / __header / __body / __footer
 *   (03-css-rules/02-atoms/dialog.css)。
 * 行为: backdrop 点击关闭 + Esc 关闭 + 打开时聚焦首个可聚焦元素。
 *   (定位/进出动画归 L7;本壳只做静态居中 + 基本可达性)
 */

import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'
import { useScrollAffordance } from '../../03-editor/lib/useScrollAffordance'

export type BaseDialogProps = {
  title: ReactNode
  children: ReactNode
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg'
  onClose: () => void
}

export function BaseDialog(props: BaseDialogProps): ReactNode {
  const t = useT()
  const { title, children, footer, size, onClose } = props
  const panelRef = useRef<HTMLDivElement>(null)
  const bodyRef = useScrollAffordance('y')

  // Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // 打开时聚焦首个可聚焦元素(input 优先)
  useEffect(() => {
    const el = panelRef.current
    if (!el) return
    // 优先聚焦输入/标注的主元素;button 放最后(否则展示类对话框焦点落在 ✕ 上)
    const focusable = el.querySelector<HTMLElement>(
      'input, textarea, select, [data-autofocus="true"]',
    ) ?? el.querySelector<HTMLElement>('button')
    focusable?.focus()
  }, [])

  // portal 到 body:.ze-dialog/__backdrop 是 position:fixed 全屏模态,但在 zone 内使用时
  // (ConfirmDialog / dialogRegistry 输入弹窗 / AiConsentGate)其祖先 .ze-editor 具 container-type
  // + backdrop-filter,会把 fixed 包含块从视口改写为编辑器面板并 overflow:clip 裁切——遮罩只盖面板、
  // 弹窗居中在面板且被裁。portal 到 body 逃离该包含块,恢复真视口全屏模态(与 ContextMenu/ImageLightbox
  // 同策略);.ze-dialog 定位走全局类不依赖祖先,Header 对话框 portal 后行为不变(无回归)。
  return createPortal(
    <>
      <div className="ze-dialog__backdrop" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        className="ze-dialog"
        data-open="true"
        data-size={size ?? undefined}
        role="dialog"
        aria-modal="true"
      >
        <div className="ze-dialog__header">
          <span className="ze-dialog__title">{title}</span>
          <button
            type="button"
            className="ze-icon-button"
            data-size="sm"
            aria-label={t('common.close')}
            onClick={onClose}
          >
            <ZeIcon name="close" />
          </button>
        </div>
        <div ref={bodyRef} className="ze-dialog__body ze-scroll-fade-y">{children}</div>
        {footer && <div className="ze-dialog__footer">{footer}</div>}
      </div>
    </>,
    document.body,
  )
}
