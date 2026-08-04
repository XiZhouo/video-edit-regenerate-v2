/**
 * DialogHost — 根据当前打开的对话框种类渲染对应组件
 *
 * SPEC: ../../SPEC.md §2.3.5-§2.3.7 + §2.6 mod 重载点(dialogs 覆盖)
 *
 * HeaderShell 持 active 状态({kind, payload})并渲染本 host(在 dispatch + dialog
 * 两个 Provider 之内)。mod 可经 overrides.dialogs[kind] 完全替换某个对话框
 * ("模板"机制);未覆盖则走 core 默认("写死")。
 */

import { type ReactNode } from 'react'

import { AboutDialog, ShortcutsDialog, ManualDialog } from './displayDialogs'
import {
  NewDialog, OpenDialog, SnapshotDialog, SaveAsDialog, RenameDialog, ExportDialog,
  ImportDialog,
} from './formDialogs'
import { DeleteDialog, CloseConfirmDialog, LoginDialog } from './confirmDialogs'
import type { DialogKind, DialogPayload } from './lib/dialogState'
import type { HeaderConfig, DocumentInfo, SyncState } from '../types'

/** mod 覆盖:某 kind 的对话框由 mod 自渲染(给 onClose) */
export type DialogOverrides = Partial<Record<DialogKind, (ctx: { onClose: () => void }) => ReactNode>>

export type DialogHostProps = {
  active: { kind: DialogKind; payload?: DialogPayload } | null
  onClose: () => void
  doc: DocumentInfo
  config?: HeaderConfig
  sync: SyncState
  aboutExtras?: Array<{ label: string; value: string }>
  overrides?: DialogOverrides
}

export function DialogHost(props: DialogHostProps): ReactNode {
  const { active, onClose, doc, config, sync, aboutExtras, overrides } = props
  if (!active) return null

  // mod 覆盖优先
  const override = overrides?.[active.kind]
  if (override) return override({ onClose })

  // header-v2:文件列表行内按钮带 payload.target({id,name})→ rename/save_as/delete
  // 作用于该行而非当前 doc;无 target 回退当前 doc(入口兼容)。
  const target = active.payload?.target as { id: string; name: string; generation: number } | undefined
  const effDoc: DocumentInfo = target
    ? doc?.id === target.id
      // 当前行必须沿用画布已渲染的 document version；列表行只有 topology
      // generation，不能用它构造一个假的 document CAS 基线。
      ? doc
      : {
        id: target.id, generation: target.generation, name: target.name, schema_version: '',
        collaboration_mode: 'live_collab', _v: 0, updated_at: 0, data_dir_path: '',
      }
    : doc

  switch (active.kind) {
    // 展示类
    case 'about':     return <AboutDialog config={config} doc={doc} extras={aboutExtras} onClose={onClose} />
    case 'shortcuts': return <ShortcutsDialog onClose={onClose} />
    case 'manual':    return <ManualDialog config={config} onClose={onClose} />
    // 表单类
    case 'new':       return <NewDialog onClose={onClose} />
    case 'open':      return <OpenDialog onClose={onClose} />
    case 'snapshot':  return <SnapshotDialog onClose={onClose} />
    case 'save_as':   return <SaveAsDialog doc={effDoc} onClose={onClose} />
    case 'rename':    return <RenameDialog doc={effDoc} onClose={onClose} />
    case 'export':    return <ExportDialog onClose={onClose} />
    case 'import':    return <ImportDialog onClose={onClose} />
    // 确认 / 销毁类
    case 'delete':    return <DeleteDialog doc={effDoc} onClose={onClose} />
    case 'close':     return <CloseConfirmDialog sync={sync} onClose={onClose} />
    case 'login':     return <LoginDialog onClose={onClose} />
    // 穷尽性检查:新增 DialogKind 未补 case 时,active.kind 不再是 never → 编译期报错
    default:
      return ((_: never) => null)(active.kind)
  }
}
