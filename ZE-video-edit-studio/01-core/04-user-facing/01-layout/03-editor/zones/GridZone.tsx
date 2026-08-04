/**
 * GridZone — 网格内容区(functional zone;SPEC §2B `grid`)
 *
 * 固定骨架(组件画死):自适应缩略网格(ThumbnailGrid)+ 卡片骨架/错误态 + 多选/框选 +
 * 拖拽重排 + 右键菜单(复制链接/图片/下载 = 资产工具;采纳/删除 = 命令)。
 * 复用现 ThumbnailGrid + `.ze-thumb*`(L2 thumb.css 组件原子)。
 * 拓展点(受约束):字段映射(thumb/score/label)+ onAdopt/reorderCommand(命令)。
 * filter-strip 为可选拓展点(build-v3 cardlist 暂未用,真要时按契约加)。
 */

import { useMemo, useState, type ReactNode } from 'react'
import { useT, useLocale } from '@ze/core/04-user-facing/05-preferences'
import { ThumbnailGrid, type ThumbItem, type ThumbnailLayoutMode, type ThumbSizeAttr } from '../primitives/ThumbnailGrid'
import { ContextMenu, type ContextMenuItem } from '../primitives/ContextMenu'
import { useContextMenu } from '../primitives/useContextMenu'
import { copyText, copyImageToClipboard, triggerDownload } from '../lib/assetActions'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import { enumOptionLabel, i18nLabel } from '../lib/fieldSchema'
import type { SelectionMode } from '../../lib/selectionModel'
import type {
  AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus, FieldSchema, LoadingStatus,
} from '../types'

export type GridZoneProps = {
  entityType: string
  entities: EntityDict[]
  /** 拓展点:字段映射(缩略图/评分/标题来自哪个 data 字段)。 */
  thumbField?: string
  scoreField?: string
  labelField?: string
  descriptionField?: string
  summaryFields?: string[]
  /** summaryFields 摘要 chip 的字段 schema(取字段人话 label / x-ze-label-i18n;缺则回退裸字段键)。 */
  fieldSchemas?: Record<string, FieldSchema>
  layoutMode?: ThumbnailLayoutMode
  /** 卡片列表抽象拓展点:连续缩略图 scale / 旧内部列宽档 / 无媒体区 / 逐卡显隐锁定字段(boolean data 字段名)。 */
  thumbScale?: number
  thumbSize?: ThumbSizeAttr
  showThumb?: boolean
  visibilityField?: string
  lockedField?: string
  selectedId?: string | null
  multiSelection?: string[]
  onSelect?: (id: string) => void
  onSelectMulti?: (id: string, mode: SelectionMode, orderedIds: string[]) => void
  assetUrl?: AssetUrlFn
  statusOf?: (id: string) => EntityRunStatus | undefined
  /** 拓展点:采纳命令(回调 by 编辑器)/ 拖拽重排命令 id。 */
  onAdopt?: (id: string) => void
  deleteCommand?: string
  reorderCommand?: string
  dispatch?: DispatchFn
  emptyText?: string
}

function asRef(v: unknown): AssetRefDict | null {
  return v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>)
    ? (v as AssetRefDict) : null
}
function pickRef(data: Record<string, unknown>, field?: string): AssetRefDict | null {
  if (field) return asRef(data[field])
  for (const v of Object.values(data)) { const r = asRef(v); if (r) return r }
  return null
}
function phaseToLoading(phase?: string): LoadingStatus | undefined {
  if (phase === 'error') return 'error'
  if (phase === 'pending' || phase === 'running') return 'loading'
  return undefined
}

export function GridZone({
  entityType, entities, thumbField, scoreField, labelField, selectedId, multiSelection,
  descriptionField, summaryFields, fieldSchemas, layoutMode, thumbScale, thumbSize, showThumb, visibilityField, lockedField,
  onSelect, onSelectMulti, assetUrl, statusOf, onAdopt, deleteCommand, reorderCommand, dispatch, emptyText,
}: GridZoneProps): ReactNode {
  const t = useT()
  const { locale } = useLocale()
  const ctxMenu = useContextMenu()
  const [menuId, setMenuId] = useState<string | null>(null)
  const [drag, setDrag] = useState<{ draggingId: string | null; dragOverId: string | null }>({ draggingId: null, dragOverId: null })
  const bodyRef = useScrollAffordance('y')

  const urlOf = (e: EntityDict): string | undefined => {
    const ref = pickRef(e.data, thumbField)
    return ref && assetUrl ? assetUrl(ref) : undefined
  }

  // 拖拽排序开启(声明了 reorderCommand)时,显示顺序跟随 order 字段(entity.reorder 重写它);
  // order 缺失的排到末尾,同值/缺失按原相对序(Array.prototype.sort 稳定)。未开启则原样插入序。
  const ordered = useMemo(() => {
    if (!reorderCommand) return entities
    return [...entities].sort((a, b) => {
      const oa = typeof a.data.order === 'number' ? (a.data.order as number) : Infinity
      const ob = typeof b.data.order === 'number' ? (b.data.order as number) : Infinity
      return oa - ob
    })
  }, [entities, reorderCommand])

  const items: ThumbItem[] = useMemo(() => ordered.map((e) => {
    const st = statusOf?.(e.id)
    return {
      id: e.id,
      // W1-2:实体身份随卡片落到 DOM(见 ThumbnailGrid 的 data-ze-entity-*)。
      // 引导的 `entity:<type>:<id>` 靠它解析;此前全舰队无人产出这对属性,
      // 于是所有 entity 目标恒解析成 null,精灵只说不指。
      entityType: e.type,
      src: urlOf(e),
      label: (labelField ? String(e.data[labelField] ?? '') : '') || e.name || e.id.slice(0, 8),
      description: descriptionField ? String(e.data[descriptionField] ?? '') : undefined,
      score: scoreField && typeof e.data[scoreField] === 'number' ? (e.data[scoreField] as number) : undefined,
      meta: (summaryFields ?? [])
        .map((field) => {
          const v = e.data[field]
          if (v === undefined || v === null || v === '') return null
          // chip 标签用字段人话 label(schema label / x-ze-label-i18n);缺声明回退裸字段键。
          const fSchema = fieldSchemas?.[field]
          const label = fSchema ? i18nLabel(fSchema, field, locale) : field
          // chip 的**值**同样要人话:枚举字段照 x-ze-enum-labels 取显示名,否则中文界面上
          // 会出现「处置 unreviewed」这种半中半英。缺声明时 enumOptionLabel 原样回退,行为不变。
          const value = typeof v === 'object'
            ? JSON.stringify(v)
            : (fSchema ? enumOptionLabel(fSchema, v, locale) : String(v))
          return { label, value }
        })
        .filter((v): v is { label: string; value: string } => !!v),
      status: phaseToLoading(st?.phase),
      runPhase: st?.phase,
      progress: st?.progress,
      error: st?.error,
      // 显隐/锁定:visible 缺省 true(false 才算隐),locked 缺省 false
      hidden: visibilityField ? e.data[visibilityField] === false : undefined,
      locked: lockedField ? e.data[lockedField] === true : undefined,
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [ordered, thumbField, scoreField, labelField, descriptionField, summaryFields, fieldSchemas, locale, statusOf, assetUrl, visibilityField, lockedField])

  const del = (id: string) => {
    const e = entities.find((x) => x.id === id)
    if (e) dispatch?.(deleteCommand ?? 'entity.delete', { type: e.type, id: e.id, expected_version: e.version })
  }

  // 显隐/锁定切换:内置 entity.update CAS(布尔字段取反;visible 缺省 true / locked 缺省 false)
  const toggleBool = (id: string, field: string, defaultVal: boolean) => {
    const e = entities.find((x) => x.id === id)
    if (!e) return
    const cur = typeof e.data[field] === 'boolean' ? (e.data[field] as boolean) : defaultVal
    dispatch?.('entity.update', { type: e.type, id: e.id, patches: { [field]: !cur }, expected_version: e.version })
  }

  const menuItems = (): (ContextMenuItem | 'separator')[] => {
    const e = menuId ? entities.find((x) => x.id === menuId) : null
    if (!e) return []
    const phase = statusOf?.(e.id)?.phase
    const processing = phase === 'pending' || phase === 'running'
    const url = urlOf(e)
    const items: (ContextMenuItem | 'separator')[] = []
    if (url) {
      items.push({ id: 'copyUrl', label: t('editor.copyUrl'), onSelect: () => copyText(url) })
      items.push({ id: 'copyImg', label: t('editor.copyImage'), onSelect: () => { void copyImageToClipboard(url, e.name || e.id) } })
      items.push({ id: 'download', label: t('editor.download'), onSelect: () => triggerDownload(url, e.name || e.id) })
      items.push('separator')
    }
    if (onAdopt) items.push({ id: 'adopt', label: t('editor.adopt'), onSelect: () => onAdopt(e.id) })
    items.push({ id: 'del', label: t('editor.delete'), variant: 'danger', disabled: processing, onSelect: () => del(e.id) })
    return items
  }

  // 滚动归属层不带内距:卡片流直接铺满(壳/边距冗余是需求方多次点名病灶);
  // 贴边由外层 __work 的 space-1 内距兜底,不再叠一层 pad。
  return (
    <div ref={bodyRef} className="ze-editor__gallery-body ze-scroll-fade-y">
      <ThumbnailGrid
        items={items}
        selectedId={selectedId}
        onSelect={onSelect}
        multiSelection={multiSelection}
        onSelectMulti={onSelectMulti}
        onContextMenu={(id, e) => { setMenuId(id); ctxMenu.onContextMenu(e) }}
        onReorder={reorderCommand ? (id, beforeId) => {
          const e = entities.find((x) => x.id === id)
          if (e) dispatch?.(reorderCommand, { type: e.type, id, before_id: beforeId })
        } : undefined}
        draggingId={drag.draggingId}
        dragOverId={drag.dragOverId}
        onDragStateChange={setDrag}
        onAdopt={onAdopt}
        onDelete={del}
        onRetry={(id) => {
          const st = statusOf?.(id)
          if (st?.run_id) dispatch?.('pipeline.retry_failed', { pipeline_id: st.run_id, item_ids: [id] })
        }}
        layoutMode={layoutMode}
        thumbScale={thumbScale}
        thumbSize={thumbSize}
        showThumb={showThumb}
        onToggleVisible={visibilityField ? (id) => toggleBool(id, visibilityField, true) : undefined}
        onToggleLock={lockedField ? (id) => toggleBool(id, lockedField, false) : undefined}
        emptyText={emptyText ?? t('editor.empty', { type: entityType })}
      />
      <ContextMenu state={ctxMenu.state} items={menuItems()} onClose={ctxMenu.close} menuRef={ctxMenu.menuRef} />
    </div>
  )
}
