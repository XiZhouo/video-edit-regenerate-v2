/**
 * AssetEditor — `asset` editor class:单资产查看(图片用视口;PDF/文本/markdown 内联预览;其他给下载)。
 *
 * gallery 的单实体兄弟:gallery 浏览多个,asset 聚焦看一个。图片走 CanvasViewport
 * (缩放/平移 + 双击 fit);PDF 走原生 <embed>(零依赖);text/* 走 <pre>;markdown 走
 * 共享 renderMarkdown(零依赖)→ prose;其余(mime 未识别)显文件信息 + 下载(yazi 式预览面复用本编辑器)。
 *
 * 深化(P6):图片右键 → 复制链接 / 复制图片 / 下载(本地 DOM,不 dispatch);旋转/翻转 →
 * transformCommand(mod 声明,重像素在 mod operation,SSE 回灌新版本)。
 */

import { useEffect, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { FunctionZone } from '../../zones/FunctionZone'
import { PreviewZone } from '../../zones/PreviewZone'
import { CanvasViewport } from '../../primitives/CanvasViewport'
import { useScrollAffordance } from '../../lib/useScrollAffordance'
import { InfoField } from '../../primitives/InspectorSection'
import { ContextMenu, type ContextMenuItem } from '../../primitives/ContextMenu'
import { useContextMenu } from '../../primitives/useContextMenu'
import { copyText, copyImageToClipboard, triggerDownload } from '../../lib/assetActions'
import { renderMarkdown } from '../../lib/markdown'
import { ZeIcon } from '../../../lib/zeIcon'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type AssetEditorProps = {
  entity: EntityDict | null
  /** 编辑器名(layout slot.nameKey 译名;优先显示槽位名,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  assetUrl?: AssetUrlFn
  status?: EntityRunStatus
  assetField?: string
  /** 旋转/翻转命令 id(mod 声明,如 `asset.transform {type,id,op}`);无则不显示变换工具 */
  transformCommand?: string
  dispatch?: DispatchFn
}

function pickRef(data: Record<string, unknown>, field?: string): AssetRefDict | null {
  const asRef = (v: unknown) => (v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? v as AssetRefDict : null)
  if (field) return asRef(data[field])
  for (const v of Object.values(data)) { const r = asRef(v); if (r) return r }
  return null
}

export function AssetEditor({ entity, editorName, assetUrl, status, assetField, transformCommand, dispatch }: AssetEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const ctxMenu = useContextMenu()

  // mime 分流(hooks 须在早返回前;entity 为空时各值降级)。
  const ref = entity ? pickRef(entity.data, assetField) : null
  const src = ref && assetUrl ? assetUrl(ref) : undefined
  const mime = ref?.mime ?? ''
  const isImage = !ref?.mime || mime.startsWith('image/')
  const isPdf = mime === 'application/pdf'
  const isMarkdown = mime === 'text/markdown' || mime === 'text/x-markdown' || ref?.ext === '.md'
  const isText = mime.startsWith('text/')
  const needsText = !!src && !isImage && !isPdf && (isText || isMarkdown)

  // text/markdown 走 assetUrl fetch 取文本(只读预览);非文本类型清空。
  const [textContent, setTextContent] = useState<string | null>(null)
  const docRef = useScrollAffordance('y')
  const actionsRef = useScrollAffordance('x')
  useEffect(() => {
    if (!needsText || !src) { setTextContent(null); return }
    let cancelled = false
    fetch(src).then((r) => r.text()).then((txt) => { if (!cancelled) setTextContent(txt) }).catch(() => { if (!cancelled) setTextContent('') })
    return () => { cancelled = true }
  }, [src, needsText])

  if (!entity) {
    return (
      <EditorFrame editorClass="asset" header={<HeaderZone title={editorName ?? t('editor.noAsset')} />}>
        <EditorFrame.Empty>{t('editor.noAsset')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }
  const phase = status?.phase

  const transform = (op: string) =>
    transformCommand && fire(transformCommand, { type: entity.type, id: entity.id, op, expected_version: entity.version })

  const menuItems = (): (ContextMenuItem | 'separator')[] => {
    if (!src) return []
    return [
      { id: 'copyUrl', label: t('editor.copyUrl'), onSelect: () => copyText(src) },
      { id: 'copyImg', label: t('editor.copyImage'), onSelect: () => { void copyImageToClipboard(src, entity.name || entity.id) } },
      { id: 'download', label: t('editor.download'), onSelect: () => triggerDownload(src, entity.name || entity.id) },
    ]
  }

  const showTransform = isImage && !!transformCommand && phase !== 'pending' && phase !== 'running' && phase !== 'error' && !!ref

  // 操作区:变换图标组(局部 transform 触发,保留图标-only 外观)。
  const transformZone = showTransform ? (
    <FunctionZone key="transform" id="toolbar" role="toolbar" kind="tool" layout="hug-scroll">
      <div ref={actionsRef} className="ze-editor__zone-actions ze-scroll-fade-x" role="group">
        <button type="button" className="ze-icon-button" data-size="sm" title={t('editor.rotate')}
          onClick={() => transform('rotate90')}><ZeIcon name="rotate" /></button>
        <button type="button" className="ze-icon-button" data-size="sm" title={t('editor.flipH')}
          onClick={() => transform('flipH')}><ZeIcon name="flip-h" /></button>
        <button type="button" className="ze-icon-button" data-size="sm" title={t('editor.flipV')}
          onClick={() => transform('flipV')}><ZeIcon name="flip-v" /></button>
      </div>
    </FunctionZone>
  ) : null

  let body: ReactNode
  if (!ref) {
    body = <div className="ze-editor__empty">{t('editor.noAssetRef')}</div>
  } else if (isImage) {
    body = <CanvasViewport src={src} />
  } else if (isPdf) {
    body = <embed className="ze-asset__pdf" src={src} type="application/pdf" />
  } else if (isMarkdown) {
    body = <div ref={docRef} className="ze-asset__doc ze-scroll-fade-y ze-pad" data-p="2"><div className="ze-text" data-prose="true" dangerouslySetInnerHTML={{ __html: renderMarkdown(textContent ?? '') }} /></div>
  } else if (isText) {
    body = <pre ref={docRef} className="ze-asset__text ze-scroll-fade-y ze-text ze-pad" data-mono="true" data-size="xs" data-p="2">{textContent ?? ''}</pre>
  } else {
    body = (
      <div className="ze-asset__file ze-stack ze-pad" data-p="4">
        <InfoField label={t('editor.type')} value={ref.mime ?? t('editor.unknown')} />
        <InfoField label={t('editor.size')} value={ref.size ? `${(ref.size / 1024).toFixed(1)} KB` : '—'} />
        {src && <a className="ze-button" data-variant="primary" data-size="sm" href={src} download>{t('editor.download')}</a>}
      </div>
    )
  }

  const objectName = entity.name || entity.id.slice(0, 8)
  const headerTitle = editorName ? `${editorName} · ${objectName}` : objectName
  return (
    <EditorFrame
      editorClass="asset"
      header={<HeaderZone title={headerTitle} />}
      zones={transformZone ? [transformZone] : undefined}
    >
      <PreviewZone
        phase={phase}
        error={status?.error}
        onRetry={status?.run_id ? () => fire('pipeline.retry_failed', { pipeline_id: status.run_id, item_ids: [entity.id] }) : undefined}
        busyStatus={status}
        onContextMenu={isImage && src ? ctxMenu.onContextMenu : undefined}
      >
        {body}
      </PreviewZone>
      <ContextMenu state={ctxMenu.state} items={menuItems()} onClose={ctxMenu.close} menuRef={ctxMenu.menuRef} />
    </EditorFrame>
  )
}
