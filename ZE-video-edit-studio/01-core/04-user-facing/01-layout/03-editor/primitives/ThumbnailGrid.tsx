/**
 * ThumbnailGrid / ThumbnailCard — 缩略图 / 候选 / 结果浏览 primitive(框架级)
 *
 * 各 skill 反复重造的"候选网格 + 评分徽章 + 采纳/删除"在此收口。GalleryEditor / AssetEditor /
 * TaskEditor(结果)复用。真空 pending 才使用骨架；running/error 保留真实内容并把运行事实
 * 装饰到卡片自身，消灭 PropertyPanel vs Ghost 的重复与“运行中整卡消失”。
 *
 * 受控:items 进,onSelect / onAdopt / onDelete 出。assetUrl 解析由上层注入(editor 不直读 store)。
 * 多选(onSelectMulti)/ 右键(onContextMenu)/ 拖拽重排(onReorder)均为加性 opt-in:
 *   不传则退回单选、无菜单、不可拖,既有调用点零改动。
 * 视觉:03-css-rules/02-atoms/thumb.css 的 .ze-thumb*(@container 响应列数)。
 */

import { useState, type CSSProperties, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ErrorPlaceholder, ProgressRing } from '../lib/Skeleton'
import {
  clampThumbScale, thumbCompactMediaWidthForScale, thumbGridMinWidthForScale, thumbListMediaWidthForScale,
} from '../lib/cardListModel'
import { ZeIcon } from '../../lib/zeIcon'
import { selectionModeFromEvent, type SelectionMode } from '../../lib/selectionModel'
import { useMarquee } from '../lib/useMarquee'
import { ImageLightbox } from './ImageLightbox'
import type { LoadingStatus, RunError, RunPhase } from '../types'

export type ThumbItem = {
  id: string
  /** 实体类型。带上它,卡片根节点才能产出 data-ze-entity-type/-id —— 引导的
   *  `entity:<type>:<id>` 目标靠这对属性解析(W1-2)。缺省=不产出(非实体卡片,如纯素材缩略)。 */
  entityType?: string
  src?: string
  /** CSS aspect-ratio value, e.g. "1024 / 1536"; image natural size fills this when absent. */
  mediaAspectRatio?: string
  label?: string
  description?: string
  score?: number
  meta?: { label: string; value: string }[]
  /** 实体级运行相位；status 仅保留旧 primitive 的 loading/error 兼容表面。 */
  runPhase?: RunPhase
  status?: LoadingStatus
  progress?: number
  error?: RunError
  /** 逐卡显隐/锁定(卡片列表抽象:绑 mod 声明的 boolean data 字段;undefined=不渲染该钮)。 */
  hidden?: boolean
  locked?: boolean
}

export type ThumbSizeAttr = 'xs' | 'sm' | 'md' | 'lg' | 'xl'

type ThumbGridStyle = CSSProperties & {
  '--ze-thumb-card-min-w'?: string
  '--ze-thumb-list-media-w'?: string
  '--ze-thumb-compact-media-w'?: string
}
type ThumbMediaStyle = CSSProperties & {
  '--ze-thumb-media-ratio'?: string
}

export type ThumbnailLayoutMode = 'grid' | 'card-list' | 'compact-list'

export type ThumbnailCardProps = {
  item: ThumbItem
  selected?: boolean
  /** 多选高亮(>1 选中时,与 selected 并存) */
  multiSelected?: boolean
  onClick?: (e: React.MouseEvent) => void
  onContextMenu?: (e: React.MouseEvent) => void
  onAdopt?: () => void
  onDelete?: () => void
  onRetry?: () => void
  /** 逐卡显隐/锁定切换(卡片列表抽象;stopPropagation,不触发选卡)。 */
  onToggleVisible?: () => void
  onToggleLock?: () => void
  /** false = 无媒体区一等公民(纯文本卡;容器同时挂 data-media="none")。 */
  showThumb?: boolean
  /** 显隐/锁定钮的 title 文案(i18n 由上层注入,primitive 不拉 useT 双份)。 */
  toggleTitles?: { show: string; hide: string; lock: string; unlock: string }
  /** 拖拽重排:draggable 才挂 DnD handler */
  draggable?: boolean
  dragging?: boolean
  dragOver?: boolean
  onDragStart?: (e: React.DragEvent) => void
  onDragEnter?: (e: React.DragEvent) => void
  onDragOver?: (e: React.DragEvent) => void
  onDrop?: (e: React.DragEvent) => void
  onDragEnd?: (e: React.DragEvent) => void
  onPreview?: () => void
  /** true = media box follows item/natural image aspect; false = fixed thumbnail box. */
  autoMediaAspect?: boolean
}

export function ThumbnailCard({
  item, selected, multiSelected, onClick, onContextMenu, onAdopt, onDelete, onRetry,
  onToggleVisible, onToggleLock, showThumb = true, toggleTitles,
  draggable, dragging, dragOver, onDragStart, onDragEnter, onDragOver, onDrop, onDragEnd,
  onPreview, autoMediaAspect = true,
}: ThumbnailCardProps): ReactNode {
  const t = useT()
  const hasContent = !!(
    item.src
    || item.label
    || item.description
    || typeof item.score === 'number'
    || item.meta?.length
  )
  // runPhase 是实体运行事实。旧调用方只给 status='loading' 时，只有真空卡才兼容为 pending
  // 骨架；已有任何内容就按 running 保留原对象，不能再用骨架把结果遮掉。
  const runPhase = item.runPhase
    ?? (item.status === 'error' ? 'error' : item.status === 'loading' ? (hasContent ? 'running' : 'pending') : undefined)
  const running = runPhase === 'running'
  const pending = runPhase === 'pending'
  const processing = pending || running
  const errored = runPhase === 'error'
  const vacantPending = pending && !hasContent
  const blockingError = errored && !hasContent
  const visualStatus: LoadingStatus = runPhase
    ? (processing ? 'loading' : errored ? 'error' : 'idle')
    : (item.status ?? 'idle')
  // 运行态只改变对象自身样式，不剥夺选择/键盘/右键；只在按钮级禁用会破坏在途目标的删除。
  const activate = onClick
  const canDrag = draggable && !vacantPending && !item.locked
  const mediaStyle: ThumbMediaStyle | undefined = autoMediaAspect && item.mediaAspectRatio
    ? { '--ze-thumb-media-ratio': item.mediaAspectRatio }
    : undefined

  // 逐卡显隐/锁定钮(stopPropagation:操作不等于选择)。媒体卡贴媒体区角落;无媒体卡行首内联。
  const toggles = (onToggleVisible || onToggleLock) && !blockingError ? (
    <span className="ze-thumb__toggles" data-inline={showThumb ? undefined : 'true'}>
      {onToggleVisible && (
        <button type="button" className="ze-icon-button" data-size="sm"
          data-active={item.hidden ? undefined : 'true'}
          title={item.hidden ? (toggleTitles?.show ?? '') : (toggleTitles?.hide ?? '')}
          onClick={(e) => { e.stopPropagation(); onToggleVisible() }}
        >{item.hidden ? <ZeIcon name="eye-off" /> : <ZeIcon name="eye" />}</button>
      )}
      {onToggleLock && (
        <button type="button" className="ze-icon-button" data-size="sm"
          data-active={item.locked ? 'true' : undefined}
          title={item.locked ? (toggleTitles?.unlock ?? '') : (toggleTitles?.lock ?? '')}
          onClick={(e) => { e.stopPropagation(); onToggleLock() }}
        >{item.locked ? <ZeIcon name="lock" /> : <ZeIcon name="unlock" />}</button>
      )}
    </span>
  ) : null

  return (
    <div
      className="ze-thumb"
      data-id={item.id}
      /* W1-2 引导锚点(框架层统一产出,mod 零配置就位):
         guideTargets.resolveGuideAnchor 的 `entity:` 分支查的正是这对属性。
         此前全仓约 180 个 skill 无任何组件产出它,读方(选择器)发货三周而写方从未建,
         于是每个 entity 目标都 resolve 成 null,GuideLayer 按设计降级成「只说不指」。 */
      data-ze-entity-type={item.entityType}
      data-ze-entity-id={item.entityType ? item.id : undefined}
      data-selected={selected ? 'true' : 'false'}
      data-multi-selected={multiSelected ? 'true' : undefined}
      data-status={visualStatus}
      data-run-phase={runPhase}
      data-hidden={item.hidden ? 'true' : undefined}
      data-locked={item.locked ? 'true' : undefined}
      data-dragging={dragging ? 'true' : undefined}
      data-drag-over={dragOver ? 'true' : undefined}
      onClick={activate}
      onContextMenu={onContextMenu}
      onKeyDown={activate ? (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(e as unknown as React.MouseEvent) }
      } : undefined}
      role="button"
      tabIndex={0}
      draggable={canDrag ? true : undefined}
      onDragStart={canDrag ? onDragStart : undefined}
      onDragEnter={draggable ? onDragEnter : undefined}
      onDragOver={draggable ? onDragOver : undefined}
      onDrop={draggable ? onDrop : undefined}
      onDragEnd={draggable ? onDragEnd : undefined}
    >
      {showThumb ? (
        <div className="ze-thumb__media" style={mediaStyle}>
          {blockingError ? (
            <div className="ze-thumb__error" onClick={(e) => {
              // 点错误正文仍选中实体；点局部重试只执行按钮动作，不连带改选择。
              if ((e.target as HTMLElement).closest('button')) e.stopPropagation()
            }}>
              <ErrorPlaceholder compact error={item.error} onRetry={onRetry} />
            </div>
          ) : vacantPending ? (
            <span className="ze-skeleton" data-variant="block" aria-hidden="true" />
          ) : item.src ? (
            <>
              <img
                className="ze-thumb__img"
                src={item.src}
                alt={item.label ?? item.id}
                loading="lazy"
                draggable={false}
                onLoad={(e) => {
                  if (!autoMediaAspect) return
                  const img = e.currentTarget
                  if (img.naturalWidth > 0 && img.naturalHeight > 0) {
                    img.parentElement?.style.setProperty('--ze-thumb-media-ratio', `${img.naturalWidth} / ${img.naturalHeight}`)
                  }
                }}
              />
              {onPreview ? (
                <button
                  type="button"
                  className="ze-icon-button ze-thumb__preview-button"
                  data-size="sm"
                  title={t('editor.zoomIn')}
                  onClick={(e) => { e.stopPropagation(); onPreview() }}
                >
                  <ZeIcon name="zoom-in" />
                </button>
              ) : null}
            </>
          ) : (
            <span className="ze-thumb__placeholder" aria-hidden="true"><ZeIcon name="image" /></span>
          )}
          {running ? (
            <span className="ze-thumb__loading"><ProgressRing progress={item.progress} size={28} /></span>
          ) : null}
          {typeof item.score === 'number' && !vacantPending && !blockingError && (
            <span className="ze-badge ze-thumb__score" data-variant="accent" data-size="sm">{item.score.toFixed(2)}</span>
          )}
          {toggles}
        </div>
      ) : toggles}
      {vacantPending
        ? <div className="ze-thumb__label"><span className="ze-skeleton" data-variant="text" /></div>
        : item.label && <div className="ze-thumb__label" title={item.label}>{item.label}</div>}
      {!vacantPending && !blockingError && item.description ? (
        <div className="ze-thumb__description" title={item.description}>{item.description}</div>
      ) : null}
      {!vacantPending && !blockingError && item.meta?.length ? (
        <div className="ze-thumb__meta">
          {item.meta.slice(0, 4).map((m) => (
            <span key={`${m.label}:${m.value}`} title={`${m.label}: ${m.value}`}>
              <b>{m.label}</b>{m.value}
            </span>
          ))}
        </div>
      ) : null}
      {errored && (hasContent || !showThumb) ? (
        <div className="ze-thumb__error" onClick={(e) => {
          if ((e.target as HTMLElement).closest('button')) e.stopPropagation()
        }}>
          <ErrorPlaceholder compact error={item.error} onRetry={onRetry} />
        </div>
      ) : null}
      {(onAdopt || onDelete) && !vacantPending && !blockingError && (
        <div className="ze-thumb__actions">
          {onAdopt && (
            <button type="button" className="ze-button" data-size="sm" data-variant="primary"
              onClick={(e) => { e.stopPropagation(); onAdopt() }}>{t('editor.adopt')}</button>
          )}
          {onDelete && (
            <button type="button" className="ze-icon-button" data-size="sm" data-variant="danger" title={t('editor.delete')}
              disabled={processing}
              onClick={(e) => { e.stopPropagation(); onDelete() }}><ZeIcon name="close" /></button>
          )}
        </div>
      )}
    </div>
  )
}

export type ThumbnailGridProps = {
  items: ThumbItem[]
  selectedId?: string | null
  onSelect?: (id: string) => void
  /** 多选(P0):传了则点击走 replace/⌘ toggle/⇧ range,优先于 onSelect */
  multiSelection?: string[]
  onSelectMulti?: (id: string, mode: SelectionMode, orderedIds: string[]) => void
  /** 右键菜单:由上层(GalleryEditor)挂 useContextMenu */
  onContextMenu?: (id: string, e: React.MouseEvent) => void
  /** 拖拽重排:把 id 落到 beforeId 之前(beforeId=null → 末尾) */
  onReorder?: (id: string, beforeId: string | null) => void
  onAdopt?: (id: string) => void
  onDelete?: (id: string) => void
  onRetry?: (id: string) => void
  /** 整体加载(还没拿到 items)时补几张骨架卡 */
  skeletonCount?: number
  emptyText?: string
  /** card editor display mode: image-first grid, rich card list, or compact single-line cards. */
  layoutMode?: ThumbnailLayoutMode
  /** 连续缩略图 scale(0-100);传入时优先于 thumbSize,由 CSS 变量自适应算列数。 */
  thumbScale?: number
  /** 旧内部列宽档(cardlist 之外直用 primitive 的兼容入口;缺省 md)。 */
  thumbSize?: ThumbSizeAttr
  /** false = 整列表无媒体区(纯文本卡;容器挂 data-media="none")。 */
  showThumb?: boolean
  /** 逐卡显隐/锁定切换(卡片列表抽象;id 回调,绑定字段语义在上层)。 */
  onToggleVisible?: (id: string) => void
  onToggleLock?: (id: string) => void
  /** 拖拽态(由上层 useState 管,grid 只读渲染) */
  draggingId?: string | null
  dragOverId?: string | null
  onDragStateChange?: (s: { draggingId: string | null; dragOverId: string | null }) => void
  /** true = media box follows item/natural image aspect; false = fixed thumbnail box. */
  autoMediaAspect?: boolean
}

export function ThumbnailGrid({
  items, selectedId, onSelect, multiSelection, onSelectMulti, onContextMenu, onReorder,
  onAdopt, onDelete, onRetry, skeletonCount = 0, emptyText,
  layoutMode = 'grid', thumbScale, thumbSize, showThumb = true, onToggleVisible, onToggleLock,
  draggingId, dragOverId, onDragStateChange, autoMediaAspect = true,
}: ThumbnailGridProps): ReactNode {
  const t = useT()
  const [preview, setPreview] = useState<ThumbItem | null>(null)
  // 框选(marquee):仅多选模式启用;空白拖框 → set/set-add 选中相交卡片。
  const marquee = useMarquee('.ze-thumb', (ids, additive) => {
    onSelectMulti?.('', additive ? 'set-add' : 'set', ids)
  })
  if (items.length === 0 && skeletonCount === 0) {
    return <div className="ze-editor__empty">{emptyText ?? t('editor.noContent')}</div>
  }
  const marqueeEnabled = !!onSelectMulti
  const skeletons: ThumbItem[] = Array.from({ length: items.length === 0 ? skeletonCount : 0 },
    (_, i) => ({ id: `__sk-${i}`, status: 'loading' }))
  const orderedIds = items.map((it) => it.id)
  const multiSet = new Set(multiSelection ?? [])
  const dnd = !!onReorder
  const normalizedThumbScale = thumbScale === undefined ? undefined : clampThumbScale(thumbScale)
  const thumbGridStyle: ThumbGridStyle | undefined = normalizedThumbScale === undefined ? undefined : {
    '--ze-thumb-card-min-w': `${thumbGridMinWidthForScale(normalizedThumbScale)}px`,
    '--ze-thumb-list-media-w': `${thumbListMediaWidthForScale(normalizedThumbScale)}px`,
    '--ze-thumb-compact-media-w': `${thumbCompactMediaWidthForScale(normalizedThumbScale)}px`,
  }

  const clickHandler = (id: string) =>
    onSelectMulti
      ? (e: React.MouseEvent) => onSelectMulti(id, selectionModeFromEvent(e), orderedIds)
      : onSelect
        ? () => onSelect(id)
        : undefined

  const toggleTitles = {
    show: t('editor.show'), hide: t('editor.hide'),
    lock: t('editor.lock'), unlock: t('editor.unlock'),
  }

  return (
    <>
      <div
        className="ze-thumb-grid"
        data-layout={layoutMode}
        data-thumb-size={normalizedThumbScale === undefined ? thumbSize : undefined}
        data-thumb-fluid={normalizedThumbScale === undefined ? undefined : 'true'}
        data-media={showThumb ? undefined : 'none'}
        style={thumbGridStyle}
        ref={marquee.ref}
        onPointerDown={marqueeEnabled ? marquee.onPointerDown : undefined}
        onPointerMove={marqueeEnabled ? marquee.onPointerMove : undefined}
        onPointerUp={marqueeEnabled ? marquee.onPointerUp : undefined}
        onPointerCancel={marqueeEnabled ? marquee.onPointerCancel : undefined}
      >
        {marqueeEnabled && marquee.box && <div className="ze-marquee" style={marquee.box} aria-hidden="true" />}
        {[
          ...items.map((item) => ({ item, synthetic: false })),
          ...skeletons.map((item) => ({ item, synthetic: true })),
        ].map(({ item: it, synthetic }) => (
          <ThumbnailCard
            key={it.id}
            item={it}
            selected={it.id === selectedId}
            multiSelected={multiSet.size > 1 && multiSet.has(it.id)}
            onClick={synthetic ? undefined : clickHandler(it.id)}
            onContextMenu={!synthetic && onContextMenu ? (e) => onContextMenu(it.id, e) : undefined}
            onAdopt={!synthetic && onAdopt ? () => onAdopt(it.id) : undefined}
            onDelete={!synthetic && onDelete ? () => onDelete(it.id) : undefined}
            onRetry={!synthetic && onRetry ? () => onRetry(it.id) : undefined}
            onToggleVisible={!synthetic && onToggleVisible ? () => onToggleVisible(it.id) : undefined}
            onToggleLock={!synthetic && onToggleLock ? () => onToggleLock(it.id) : undefined}
            showThumb={showThumb}
            toggleTitles={toggleTitles}
            autoMediaAspect={autoMediaAspect}
            draggable={!synthetic && dnd}
            dragging={draggingId === it.id}
            dragOver={dragOverId === it.id}
            onPreview={it.src ? () => setPreview(it) : undefined}
            onDragStart={dnd ? (e) => {
              e.dataTransfer.effectAllowed = 'move'
              e.dataTransfer.setData('text/plain', it.id)
              onDragStateChange?.({ draggingId: it.id, dragOverId: null })
            } : undefined}
            onDragEnter={dnd ? (e) => {
              e.preventDefault()
              if (draggingId && draggingId !== it.id) onDragStateChange?.({ draggingId, dragOverId: it.id })
            } : undefined}
            onDragOver={dnd ? (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' } : undefined}
            onDrop={dnd ? (e) => {
              e.preventDefault()
              const src = draggingId ?? e.dataTransfer.getData('text/plain')
              if (src && src !== it.id) onReorder!(src, it.id)
              onDragStateChange?.({ draggingId: null, dragOverId: null })
            } : undefined}
            onDragEnd={dnd ? () => onDragStateChange?.({ draggingId: null, dragOverId: null }) : undefined}
          />
        ))}
      </div>
      <ImageLightbox
        open={!!preview?.src}
        src={preview?.src}
        alt={preview?.label ?? preview?.id}
        onClose={() => setPreview(null)}
      />
    </>
  )
}
