/**
 * defaultSections — 属性面板「自动排版」(auto编排)纯逻辑。
 *
 * 当 mod 未声明 sections 但开了 autoSections 时,由 entity 的 schema + 当前 data 派生一组
 * 信息+控制单元(InspectorSectionSpec),让选中的 entity 不用手写 sections 也能得到合理排版:
 *   - schema.format==='asset' 的字段 → asset 单元(可上传挂载,AssetView 按 mime 预览;accept 限格式)
 *   - asset_ref 字段(值含 sha256,但无 format 声明)→ preview 单元(只读图片预览,向后兼容)
 *   - schema.readOnly===true 的字段 → readonly-kv 单元(系统/只读档)
 *   - 其余可编标量 → fields 单元(可编辑)
 * 单元次序:预览(视觉)→ 可编字段 → 只读档。经 InfoControlUnit 渲染 → 白拿 L2 CSS 合规。
 *
 * 纯函数(无副作用、不读 store),便于 vitest;mod 声明了 sections 则不走本路径(向后兼容)。
 */

import type { EntityDict, EntityTypeSchema } from '../types'
import type { InspectorSectionSpec } from '../zones/types'

function isAssetRef(v: unknown): boolean {
  return !!v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>)
}

export function defaultSections(
  schema: EntityTypeSchema | undefined,
  entity: EntityDict,
): InspectorSectionSpec[] {
  const props = schema?.properties
  const data = (entity.data ?? {}) as Record<string, unknown>
  // 字段全集:有 schema 用 schema.properties(稳定序),否则退回 data keys(逃生舱)。
  const fieldNames = props ? Object.keys(props) : Object.keys(data)

  const uploadFields: string[] = []   // 显式 format:'asset' → 可上传挂载(editable)
  const previewFields: string[] = []  // 仅按值识别的 asset_ref → 只读预览(向后兼容,不臆测可写)
  const readonlyFields: string[] = []
  const editableFields: string[] = []
  for (const f of fieldNames) {
    const fs = (props?.[f] ?? {}) as { format?: string; readOnly?: boolean }
    if (fs.format === 'asset') { uploadFields.push(f); continue }
    if (isAssetRef(data[f])) { previewFields.push(f); continue }
    if (fs.readOnly === true) { readonlyFields.push(f); continue }
    editableFields.push(f)
  }

  const out: InspectorSectionSpec[] = []
  for (const f of uploadFields) {
    const fs = (props?.[f] ?? {}) as { accept?: string; label?: string }
    // 可上传 asset 单元:点/拖放挂任意格式(accept 限定);AssetView 按 mime 预览。title=label/字段名。
    out.push({ id: `auto-asset-${f}`, titleKey: 'editor.edit', title: fs.label ?? f, kind: 'asset', assetField: f, accept: fs.accept })
  }
  for (const f of previewFields) {
    // title=字段名(数据直显,消歧多个预览);titleKey 兜底走 i18n。
    out.push({ id: `auto-preview-${f}`, titleKey: 'editor.preview', title: f, kind: 'preview', previewField: f })
  }
  if (editableFields.length) {
    out.push({ id: 'auto-fields', titleKey: 'editor.edit', kind: 'fields', fields: editableFields })
  }
  if (readonlyFields.length) {
    out.push({ id: 'auto-readonly', titleKey: 'editor.advanced', kind: 'readonly-kv', fields: readonlyFields, defaultOpen: false, collapsible: true })
  }
  return out
}
