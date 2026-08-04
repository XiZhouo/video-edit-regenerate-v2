/**
 * formDialogs — 收集输入的对话框,确认时 dispatch command
 *
 * SPEC: ../../SPEC.md §2.3.5 文件菜单
 *   New 5.1 / Open 5.2 / Snapshot 5.3 / SaveAs 5.4 / Rename 5.8 / Export 5.5
 *
 * 模式:本地 state 收集输入 → 确认 dispatch(CMD, payload) + onClose;非法时禁用确定。
 * 注:后端 document.* handler 是后续批次;本批负责对话框 UI + dispatch 接线。
 * dispatch 经 useHeaderDispatch()(HeaderShell 注入;模板态走 stub)。
 */

import { useState, type CSSProperties, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { BaseDialog } from './BaseDialog'
import { useHeaderDispatch } from '../lib/dispatch'
import {
  CMD_DOCUMENT_NEW, CMD_DOCUMENT_OPEN, CMD_DOCUMENT_SNAPSHOT,
  CMD_DOCUMENT_SAVE_AS, CMD_DOCUMENT_RENAME, CMD_DOCUMENT_EXPORT,
  CMD_MANIFEST_IMPORT,
} from '../commands'
import type { DocumentInfo } from '../types'

// 视觉单一真相源:字段标签走 .ze-text[data-role=secondary][data-size=xs];FIELD 只留结构(布局)。
// 禁止内联视觉(css_layer_guard 规则 8)。
const FIELD: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 'var(--ze-space-1)', marginBottom: 'var(--ze-space-3)' }

function Footer({ onClose, onOk, okLabel, okDisabled, okVariant }: {
  onClose: () => void; onOk: () => void; okLabel: string; okDisabled?: boolean; okVariant?: string
}): ReactNode {
  const t = useT()
  return (
    <>
      <button className="ze-button" data-size="sm" onClick={onClose}>{t('common.cancel')}</button>
      <button
        className="ze-button"
        data-variant={okVariant ?? 'primary'}
        data-size="sm"
        disabled={okDisabled}
        onClick={onOk}
      >
        {okLabel}
      </button>
    </>
  )
}


// ============================================================
// 5.1 新建
// ============================================================
export function NewDialog({ onClose }: { onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [name, setName] = useState(() => t('dialog.new.defaultName'))
  const ok = () => { dispatch(CMD_DOCUMENT_NEW, { name: name.trim() }); onClose() }
  return (
    <BaseDialog title={t('dialog.new.title')} size="sm" onClose={onClose}
      footer={<Footer onClose={onClose} onOk={ok} okLabel={t('dialog.new.ok')} okDisabled={!name.trim()} />}>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.new.nameLabel')}</span>
        <input className="ze-input" value={name} onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && name.trim()) ok() }} />
      </label>
    </BaseDialog>
  )
}


// ============================================================
// 5.2 打开(M1:列表来自批 4 useRecentProjects;暂以 doc_id 直开兜底)
// ============================================================
export function OpenDialog({ onClose }: { onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [docId, setDocId] = useState('')
  const ok = () => { if (docId.trim()) { dispatch(CMD_DOCUMENT_OPEN, { doc_id: docId.trim() }); onClose() } }
  return (
    <BaseDialog title={t('dialog.open.title')} size="sm" onClose={onClose}
      footer={<Footer onClose={onClose} onOk={ok} okLabel={t('dialog.open.ok')} okDisabled={!docId.trim()} />}>
      <div className="ze-callout" data-variant="info" style={{ marginBottom: 'var(--ze-space-3)' }}>
        <span className="ze-callout__icon ze-icon" data-icon="info" aria-hidden="true" />
        <div className="ze-callout__body">{t('dialog.open.hint')}</div>
      </div>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">doc_id</span>
        <input className="ze-input" value={docId} placeholder={t('dialog.open.placeholder')}
          onChange={(e) => setDocId(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') ok() }} />
      </label>
    </BaseDialog>
  )
}


// ============================================================
// 5.3 创建快照
// ============================================================
function defaultSnapshotLabel(): string {
  // M1 给可读时间戳起点;config.snapshot_label_format 的格式化留后续批次
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function SnapshotDialog({ onClose }: { onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [label, setLabel] = useState(() => defaultSnapshotLabel())
  const [desc, setDesc] = useState('')
  const ok = () => { dispatch(CMD_DOCUMENT_SNAPSHOT, { label: label.trim(), description: desc.trim() || undefined }); onClose() }
  return (
    <BaseDialog title={t('dialog.snapshot.title')} size="sm" onClose={onClose}
      footer={<Footer onClose={onClose} onOk={ok} okLabel={t('dialog.snapshot.ok')} okDisabled={!label.trim()} />}>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.snapshot.nameLabel')}</span>
        <input className="ze-input" value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.snapshot.descLabel')}</span>
        <textarea className="ze-textarea" value={desc} onChange={(e) => setDesc(e.target.value)} />
      </label>
    </BaseDialog>
  )
}


// ============================================================
// 5.4 另存为
// ============================================================
export function SaveAsDialog({ doc, onClose }: { doc: DocumentInfo; onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [docId, setDocId] = useState(() => (doc ? `${doc.id}-copy` : ''))
  const [name, setName] = useState(() => (doc ? `${doc.name}${t('dialog.saveAs.copySuffix')}` : ''))
  // src_doc_id 随载:另存为 = 以该 doc 为底本带名复制(header-v2 行级语义;App 拦截走 duplicate 端点)
  const ok = () => { dispatch(CMD_DOCUMENT_SAVE_AS, {
    src_doc_id: doc?.id,
    doc_id: docId.trim(),
    name: name.trim(),
    expected_generation: doc?.generation,
  }); onClose() }
  return (
    <BaseDialog title={t('dialog.saveAs.title')} size="sm" onClose={onClose}
      footer={<Footer onClose={onClose} onOk={ok} okLabel={t('dialog.saveAs.ok')} okDisabled={!docId.trim() || !name.trim()} />}>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.saveAs.docIdLabel')}</span>
        <input className="ze-input" value={docId} onChange={(e) => setDocId(e.target.value)} />
      </label>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.saveAs.nameLabel')}</span>
        <input className="ze-input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
    </BaseDialog>
  )
}


// ============================================================
// 5.8 重命名(预填 + 自动全选)
// ============================================================
export function RenameDialog({ doc, onClose }: { doc: DocumentInfo; onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [name, setName] = useState(() => doc?.name ?? '')
  // doc_id 随载:行级目标(header-v2)指向该行;App 拦截按 doc_id 路由项目
  const ok = () => { if (name.trim()) { dispatch(CMD_DOCUMENT_RENAME, {
    doc_id: doc?.id,
    name: name.trim(),
    expected_generation: doc?.generation,
    // 当前项目取画布已渲染版本；非当前行由 App 先 GET 目标 snapshot 后替换为
    // 该 snapshot 的 version，再发唯一一次 CAS mutation。
    expected_version: doc?._v,
  }); onClose() } }
  return (
    <BaseDialog title={t('dialog.rename.title')} size="sm" onClose={onClose}
      footer={<Footer onClose={onClose} onOk={ok} okLabel={t('dialog.rename.ok')} okDisabled={!name.trim()} />}>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.rename.nameLabel')}</span>
        <input className="ze-input" value={name} autoFocus
          onFocus={(e) => e.target.select()}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && name.trim()) ok() }} />
      </label>
    </BaseDialog>
  )
}


// ============================================================
// 5.5 导出(.ze-pack 信封:全部实体 + 引用资产;域由库注解自动推断)
// ============================================================
const PACK_DOMAINS = ['vision', 'timeline', 'scene', 'layers', 'text', 'dataset', 'graph'] as const

export function ExportDialog({ onClose }: { onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [domain, setDomain] = useState('')   // '' = 自动(库注解推断;无注解的手写 mod 需显式选)
  const ok = () => { dispatch(CMD_DOCUMENT_EXPORT, domain ? { domain } : {}); onClose() }
  return (
    <BaseDialog title={t('dialog.export.title')} size="sm" onClose={onClose}
      footer={<Footer onClose={onClose} onOk={ok} okLabel={t('dialog.export.ok')} />}>
      <p className="ze-text" data-role="secondary" data-size="sm">{t('dialog.export.zePack')}</p>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.export.domainLabel')}</span>
        <div className="ze-select">
          <select className="ze-select__control" value={domain} onChange={(e) => setDomain(e.target.value)}>
            <option value="">{t('dialog.export.domainAuto')}</option>
            {PACK_DOMAINS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
      </label>
    </BaseDialog>
  )
}


// ============================================================
// 5.5b 导入(.ze-pack;文件对象进 payload,App 在 dispatch 缝上传转 args.asset)
// ============================================================
export function ImportDialog({ onClose }: { onClose: () => void }): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [file, setFile] = useState<File | null>(null)
  const ok = () => { if (file) { dispatch(CMD_MANIFEST_IMPORT, { file }); onClose() } }
  return (
    <BaseDialog title={t('dialog.import.title')} size="sm" onClose={onClose}
      footer={<Footer onClose={onClose} onOk={ok} okLabel={t('dialog.import.ok')} okDisabled={!file} />}>
      <label style={FIELD}>
        <span className="ze-text" data-role="secondary" data-size="xs">{t('dialog.import.fileLabel')}</span>
        <input className="ze-input" type="file" accept=".ze-pack,application/zip,.json"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      </label>
      <p className="ze-text" data-role="secondary" data-size="xs">{t('dialog.import.hint')}</p>
    </BaseDialog>
  )
}
