/**
 * ImageLightbox — image preview overlay shared by thumbnail and asset primitives.
 */

import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'

export type ImageLightboxProps = {
  src?: string
  alt?: string
  open: boolean
  onClose: () => void
}

export function ImageLightbox({ src, alt, open, onClose }: ImageLightboxProps): ReactNode {
  const t = useT()

  useEffect(() => {
    if (!open) return undefined
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open || !src || typeof document === 'undefined') return null

  return createPortal(
    <div
      className="ze-image-lightbox"
      role="dialog"
      aria-modal="true"
      onClick={(e) => { e.stopPropagation(); onClose() }}
    >
      <button
        type="button"
        className="ze-icon-button ze-image-lightbox__close"
        data-size="sm"
        title={t('common.close')}
        onClick={(e) => { e.stopPropagation(); onClose() }}
      >
        <ZeIcon name="close" />
      </button>
      <img className="ze-image-lightbox__img" src={src} alt={alt ?? ''} onClick={(e) => e.stopPropagation()} />
    </div>,
    document.body,
  )
}
