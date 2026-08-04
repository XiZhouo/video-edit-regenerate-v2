/**
 * AssetView — 按 mime 分流的资产预览原语 (03-editor/primitives).
 *
 * image→<img> · video→<video> · audio→<audio> · pdf→<embed> · text/markdown→<pre>/renderMarkdown ·
 * model/3d(glb/obj/ply…)→cube 文件卡 · 其它→类型图标 + 文件名 + 大小。纯展示,不写库。
 * AssetRefInput / kind:'asset' section 复用;现有 PreviewInfo/thumb 后续可渐进采用。
 * compact:小预览(属性面板控件内)——pdf/text 退化为图标卡,不内联 embed/pre。
 */

import { useEffect, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { renderMarkdown } from '../lib/markdown'
import { ZeIcon } from '../../lib/zeIcon'
import { ImageLightbox } from './ImageLightbox'
import type { AssetRefDict, AssetUrlFn } from '../types'

export type AssetKind = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'markdown' | 'model' | 'file'

const MODEL_EXTS = ['.glb', '.gltf', '.obj', '.ply', '.stl', '.fbx']

/** mime(优先)+ ext(兜底)→ 资产种类。mime 缺省按图(与 AssetEditor 一致)。 */
export function assetKindOf(mime: string | undefined, ext: string | undefined): AssetKind {
  const m = (mime ?? '').toLowerCase()
  const e = (ext ?? '').toLowerCase()
  if (!m || m.startsWith('image/')) return 'image'
  if (m.startsWith('video/')) return 'video'
  if (m.startsWith('audio/')) return 'audio'
  if (m === 'application/pdf') return 'pdf'
  if (m === 'text/markdown' || m === 'text/x-markdown' || e === '.md') return 'markdown'
  if (m.startsWith('text/') || m === 'application/json') return 'text'
  if (m.startsWith('model/') || MODEL_EXTS.includes(e)) return 'model'
  return 'file'
}

function iconFor(kind: AssetKind, ref: AssetRefDict): string {
  if (kind === 'model') return 'cube'
  if (kind === 'pdf') return 'pdf'
  if (kind === 'video') return 'video'
  if (kind === 'audio') return 'audio'
  if (kind === 'image') return 'image'
  if (ref.mime === 'application/zip' || ref.ext === '.zip') return 'archive'
  return 'file'
}

function fmtSize(n: number | undefined): string {
  if (!n) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export type AssetViewProps = {
  value: AssetRefDict | null
  assetUrl?: AssetUrlFn
  /** 紧凑模式:pdf/text 退化为图标卡(属性面板小预览)。 */
  compact?: boolean
}

export function AssetView({ value, assetUrl, compact }: AssetViewProps): ReactNode {
  const t = useT()
  const src = value && assetUrl ? assetUrl(value) : undefined
  const kind = assetKindOf(value?.mime, value?.ext)
  const needsText = !!src && !compact && (kind === 'text' || kind === 'markdown')

  const [text, setText] = useState<string | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  useEffect(() => {
    if (!needsText || !src) { setText(null); return }
    let cancelled = false
    fetch(src).then((r) => r.text()).then((tx) => { if (!cancelled) setText(tx) }).catch(() => { if (!cancelled) setText('') })
    return () => { cancelled = true }
  }, [src, needsText])

  if (!value || !src) return null

  if (kind === 'image') {
    return (
      <span className="ze-asset-input__image-wrap">
        <img className="ze-asset-input__preview" src={src} alt="" loading="lazy" />
        <button
          type="button"
          className="ze-icon-button ze-asset-input__preview-button"
          data-size="sm"
          title={t('editor.zoomIn')}
          onClick={(e) => { e.stopPropagation(); setPreviewOpen(true) }}
        >
          <ZeIcon name="zoom-in" />
        </button>
        <ImageLightbox open={previewOpen} src={src} onClose={() => setPreviewOpen(false)} />
      </span>
    )
  }
  if (kind === 'video') return <video className="ze-asset-input__preview" src={src} controls preload="metadata" />
  if (kind === 'audio') return <audio src={src} controls preload="metadata" />
  if (!compact && kind === 'pdf') return <embed className="ze-asset-input__preview" src={src} type="application/pdf" />
  if (!compact && kind === 'markdown') return <div className="ze-text" data-prose="true" dangerouslySetInnerHTML={{ __html: renderMarkdown(text ?? '') }} />
  if (!compact && kind === 'text') return <pre className="ze-text" data-mono="true" data-size="xs">{text ?? ''}</pre>

  // 文件卡(model/file/zip,或 compact 下的 pdf/text):类型图标 + 标签 + 大小
  return (
    <span className="ze-asset-input__file">
      <ZeIcon name={iconFor(kind, value)} />
      <span className="ze-asset-input__file-name">{value.ext ? `${kind}${value.ext}` : kind}</span>
      {value.size ? <span>{fmtSize(value.size)}</span> : null}
    </span>
  )
}
