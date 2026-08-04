/**
 * AssetRefInput — 资产挂载控件原语 (03-editor/primitives).
 *
 * dropzone(点 / 拖放 / 粘贴文件)→ uploadAsset → onChange(ref);预览经 AssetView 按 mime 分流;
 * 清除 / 替换 → onChange(null|新 ref)。多图模式追加 / 替换 asset_ref[]。受控件:value 进、onChange 出,
 * 多图写回串行等待父级 value 回灌后再发下一次 onChange,避免连续粘贴用旧 expected_version 覆盖/冲突。
 * uploadAsset 缺省或 readOnly → 降级只读(只显预览,无落区/按钮),对齐 SPEC
 * 「不提供则图元不接受上传」。accept 按字段限定格式(image/* · video/* · .glb … 省=任意;
 * 后端 AssetStore 格式无关,限制只在 UI)。上传 mime 用 file.type||octet-stream(不臆造 image/png)。
 */

import { useEffect, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type DragEvent, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'
import { AssetView } from './AssetView'
import { ThumbnailGrid, type ThumbItem } from './ThumbnailGrid'
import type { AssetRefDict, AssetUrlFn } from '../types'

type UploadAssetFn = (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
type PickMode = 'append' | 'replace'

function asAssetRefDict(v: unknown): AssetRefDict | null {
  return v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? (v as AssetRefDict) : null
}

function asAssetRefList(v: unknown): AssetRefDict[] {
  if (Array.isArray(v)) return v.map(asAssetRefDict).filter((r): r is AssetRefDict => !!r)
  const one = asAssetRefDict(v)
  return one ? [one] : []
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

function filesOf(files: FileList | File[] | null | undefined): File[] {
  return files ? Array.from(files) : []
}

function keyOfRefs(refs: AssetRefDict[]): string {
  return refs.map((ref) => ref.sha256).join('\n')
}

function clipboardFiles(data: DataTransfer | null | undefined): File[] {
  const direct = filesOf(data?.files)
  if (direct.length) return direct
  return Array.from(data?.items ?? [])
    .map((item) => item.kind === 'file' ? item.getAsFile() : null)
    .filter((file): file is File => !!file)
}

function acceptsFile(file: File, accept: string | undefined): boolean {
  const rules = (accept ?? '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean)
  if (!rules.length) return true
  const mime = (file.type || '').toLowerCase()
  const name = (file.name || '').toLowerCase()
  return rules.some((rule) => {
    if (rule.endsWith('/*')) return mime.startsWith(rule.slice(0, -1))
    if (rule.startsWith('.')) return name.endsWith(rule)
    return mime === rule
  })
}

function fallbackPickMode(multiple: boolean): PickMode {
  return multiple ? 'append' : 'replace'
}

type AssetRefInputBaseProps = {
  assetUrl?: AssetUrlFn
  uploadAsset?: UploadAssetFn
  readOnly?: boolean
  /** 接受格式(原生 file input accept;省=任意)。 */
  accept?: string
}

type AssetRefInputSingleProps = AssetRefInputBaseProps & {
  multiple?: false
  value: AssetRefDict | null
  onChange: (ref: AssetRefDict | null) => void | Promise<unknown>
}

type AssetRefInputMultiProps = AssetRefInputBaseProps & {
  multiple: true
  value: AssetRefDict[] | AssetRefDict | null
  onChange: (refs: AssetRefDict[]) => void | Promise<unknown>
}

export type AssetRefInputProps = AssetRefInputSingleProps | AssetRefInputMultiProps

export function AssetRefInput(props: AssetRefInputProps): ReactNode {
  const t = useT()
  const { assetUrl, uploadAsset, readOnly, accept } = props
  const multiple = props.multiple === true
  const propRefs = multiple ? asAssetRefList(props.value) : []
  const [localRefs, setLocalRefs] = useState<AssetRefDict[]>(propRefs)
  const refs = multiple ? localRefs : []
  const singleRef = multiple ? null : asAssetRefDict(props.value)
  const hasValue = multiple ? refs.length > 0 : !!singleRef
  const rootRef = useRef<HTMLDivElement | null>(null)
  const appendInputRef = useRef<HTMLInputElement | null>(null)
  const replaceInputRef = useRef<HTMLInputElement | null>(null)
  const hoverRef = useRef(false)
  const busyRef = useRef(false)
  const refsRef = useRef<AssetRefDict[]>(propRefs)
  const writingKeyRef = useRef<string | null>(null)
  const queuedRefsRef = useRef<AssetRefDict[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [dragover, setDragover] = useState(false)
  const canUpload = !!uploadAsset && !readOnly
  const propRefsKey = keyOfRefs(propRefs)

  const emitRefs = (next: AssetRefDict[]) => {
    if (props.multiple) return props.onChange(next)
    return props.onChange(next[0] ?? null)
  }

  const releaseBusy = () => {
    busyRef.current = false
    setBusy(false)
  }

  const setOptimisticRefs = (next: AssetRefDict[]) => {
    refsRef.current = next
    setLocalRefs(next)
  }

  const startRefsWrite = (next: AssetRefDict[]) => {
    writingKeyRef.current = keyOfRefs(next)
    void Promise.resolve(emitRefs(next)).catch(() => {
      if (writingKeyRef.current === keyOfRefs(next)) writingKeyRef.current = null
    })
  }

  const queueRefsWrite = (next: AssetRefDict[]) => {
    setOptimisticRefs(next)
    if (!props.multiple) {
      void emitRefs(next)
      return
    }
    if (writingKeyRef.current) {
      queuedRefsRef.current = next
      return
    }
    startRefsWrite(next)
  }

  const clear = () => {
    releaseBusy()
    if (props.multiple) queueRefsWrite([])
    else props.onChange(null)
  }

  const ingestFiles = async (incoming: File[], mode: PickMode) => {
    if (!incoming.length || !uploadAsset || busyRef.current) return
    const files = incoming.filter((file) => acceptsFile(file, accept))
    if (!files.length) return
    busyRef.current = true
    setBusy(true)
    const uploaded: AssetRefDict[] = []
    try {
      for (const file of files) {
        const buf = await file.arrayBuffer()
        uploaded.push(await uploadAsset(new Uint8Array(buf), file.type || 'application/octet-stream', extOf(file.name)))
      }
    } catch {
      // 上传失败不抛(保持控件可用;宿主可经 toast/run 态报错)
    } finally {
      if (uploaded.length) {
        const base = multiple && mode === 'append' ? refsRef.current : []
        const next = [...base, ...uploaded]
        queueRefsWrite(next)
      }
      releaseBusy()
    }
  }

  const ingestClipboard = (data: DataTransfer | null | undefined): boolean => {
    if (busyRef.current) return false
    const files = clipboardFiles(data)
    if (!files.length) return false
    const accepted = files.filter((file) => acceptsFile(file, accept))
    if (!accepted.length) return false
    void ingestFiles(accepted, multiple ? 'append' : 'replace')
    return true
  }

  useEffect(() => {
    if (!canUpload || typeof window === 'undefined') return undefined
    const onWindowPaste = (e: ClipboardEvent) => {
      if (e.defaultPrevented) return
      const root = rootRef.current
      const active = typeof document === 'undefined' ? null : document.activeElement
      const armed = !!root && (hoverRef.current || (!!active && root.contains(active)))
      if (!armed || !ingestClipboard(e.clipboardData)) return
      e.preventDefault()
    }
    window.addEventListener('paste', onWindowPaste)
    return () => window.removeEventListener('paste', onWindowPaste)
  })

  useEffect(() => {
    if (!multiple) {
      refsRef.current = []
      setLocalRefs([])
      writingKeyRef.current = null
      queuedRefsRef.current = null
      return undefined
    }
    const writingKey = writingKeyRef.current
    if (writingKey) {
      if (writingKey === propRefsKey) {
        writingKeyRef.current = null
        const queued = queuedRefsRef.current
        queuedRefsRef.current = null
        if (queued && keyOfRefs(queued) !== propRefsKey) {
          setOptimisticRefs(queued)
          startRefsWrite(queued)
        } else {
          setOptimisticRefs(propRefs)
        }
      }
      return undefined
    }
    setOptimisticRefs(propRefs)
    return undefined
  }, [multiple, propRefsKey])

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragover(false)
    if (canUpload) void ingestFiles(filesOf(e.dataTransfer.files), multiple ? 'append' : 'replace')
  }

  const onPaste = (e: ReactClipboardEvent<HTMLDivElement>) => {
    if (!canUpload || !ingestClipboard(e.clipboardData)) return
    e.preventDefault()
  }

  const pick = (mode: PickMode = fallbackPickMode(multiple)) => {
    if (busyRef.current) return
    const input = mode === 'replace' && multiple ? replaceInputRef.current : appendInputRef.current
    input?.click()
  }

  const onFileChange = (input: HTMLInputElement, mode: PickMode) => {
    void ingestFiles(filesOf(input.files), mode)
    input.value = ''
  }

  const removeAt = (index: number) => {
    if (!props.multiple) return
    const next = refsRef.current.filter((_, i) => i !== index)
    queueRefsWrite(next)
  }

  const multiItems: ThumbItem[] = refs.map((ref, i) => ({
    id: String(i),
    src: assetUrl ? assetUrl(ref) : undefined,
  }))

  return (
    <div
      ref={rootRef}
      className="ze-asset-input"
      data-state={hasValue ? 'filled' : 'empty'}
      data-dragover={dragover ? 'true' : undefined}
      data-busy={busy ? 'true' : undefined}
      data-disabled={!canUpload ? 'true' : undefined}
      onMouseEnter={() => { hoverRef.current = true }}
      onMouseLeave={() => { hoverRef.current = false }}
    >
      <div
        className="ze-asset-input__frame"
        role={canUpload ? 'button' : undefined}
        tabIndex={canUpload ? 0 : undefined}
        title={canUpload ? t('editor.assetPasteHint') : undefined}
        onClick={canUpload ? () => pick() : undefined}
        onKeyDown={canUpload ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick() } } : undefined}
        onPaste={canUpload ? onPaste : undefined}
        onDragOver={canUpload ? (e) => { e.preventDefault(); setDragover(true) } : undefined}
        onDragLeave={canUpload ? () => setDragover(false) : undefined}
        onDrop={canUpload ? onDrop : undefined}
      >
        {multiple && refs.length ? (
          <ThumbnailGrid
            items={multiItems}
            layoutMode="grid"
            thumbSize="md"
            autoMediaAspect={false}
            onDelete={canUpload ? (id) => removeAt(Number(id)) : undefined}
          />
        ) : singleRef ? (
          <AssetView value={singleRef} assetUrl={assetUrl} compact />
        ) : (
          <span className="ze-asset-input__hint">
            <ZeIcon name="upload" />
            {canUpload ? t('editor.assetDrop') : t('editor.noAssetRef')}
          </span>
        )}
        {canUpload && (
          <input
            ref={appendInputRef}
            className="ze-asset-input__file-field"
            type="file"
            multiple={multiple}
            accept={accept}
            onChange={(e) => onFileChange(e.currentTarget, fallbackPickMode(multiple))}
          />
        )}
      </div>
      {canUpload && multiple ? (
        <input
          ref={replaceInputRef}
          className="ze-asset-input__file-field"
          type="file"
          multiple
          accept={accept}
          onChange={(e) => onFileChange(e.currentTarget, 'replace')}
        />
      ) : null}
      {canUpload && (
        <div className="ze-asset-input__actions">
          <button type="button" className="ze-button" data-variant="ghost" data-size="sm" onClick={() => pick(multiple ? 'append' : 'replace')}>
            {hasValue && !multiple ? t('editor.assetReplace') : t('editor.assetAdd')}
          </button>
          {hasValue && multiple ? (
            <button type="button" className="ze-button" data-variant="ghost" data-size="sm" onClick={() => pick('replace')}>{t('editor.assetReplace')}</button>
          ) : null}
          {hasValue ? (
            <button type="button" className="ze-button" data-variant="ghost" data-size="sm" onClick={clear}>{t('editor.clear')}</button>
          ) : null}
        </div>
      )}
    </div>
  )
}
