/**
 * FilterBarZone — ManageZone 兼容实现(视图类·搜索/筛选;03-zones/SPEC §manage)
 *
 * 与 ToolZone(工具/命令)分工:ManageZone 收**查看辅助 + 选择管理**
 * —— 状态 pill 单选(收窄可见集),改的是 view_state(即时、可逆、无 confirm),不派发命令、不写领域。
 * 渲染 .ze-editor__zone[data-function-zone=manage][data-role=filterbar][data-layout=wrap-cluster](pill 多则换行成簇,行高
 * 兜底 row-h;几何令牌住 L2 editor-shell.css)。每组:组名(i18n)+「全部」+ 逐值 pill。
 * pill 单选语义:点已选值=取消(回全部),点新值=切到该值;一组一选(单维筛选)。
 * filters 为空则整行不渲(EditorFrame 调用方按 filters.length 决定是否塞此 zone)。
 *
 * pill 视觉 = `.ze-chip`(未选 data-interactive line-soft 轮廓 / 选中 data-selected accent 反相);
 * 元素保持 <button type="button"> 留住原生键盘可达 + :focus-visible 焦点环,只换视觉类。
 *
 * 溢出降级(行>组>件容器规则):隐藏测量层以真实 wrap 布局恒渲完整 pill 组(与 zone 同宽),
 * 其高度超 2 行 → pill 组塌成单个「筛选(N)」secondary 按钮,点开 popover 面板(portal+视口
 * clamp,面板内仍是同一 chip 双态)。测量走 ResizeObserver(容器宽/值集变都重判);无 RO 环境保持内联。
 *
 * 拓展点(受约束):filters:GridFilter[](字段映射 + i18n 组名)+ 值集来源 entities(派生去重)。
 * 选择态 pills(viewKey→value)由编辑器持有,经 viewState/onViewChange 透传(视图态不 dispatch)。
 */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { filterValues } from '../lib/cardListModel'
import type { CardSort, GridFilter } from './types'
import type { EntityDict } from '../types'

export interface FilterBarZoneProps {
  /** 状态 pill 筛选组(values 缺省按字段去重派生)。 */
  filters: GridFilter[]
  /** 派生值集的数据源(filterValues 据此对 filter.field 去重)。 */
  entities: EntityDict[]
  /** 当前每组选中值(filter.id → value | null;null/缺省 = 全部)。编辑器持有。 */
  pills: Record<string, string | null>
  /** pill 切换:编辑器据此改 view_state(即时无 confirm)。 */
  onPillChange: (filterId: string, value: string | null) => void
  /** 可选搜索框(并入筛选区 —— 搜索本质是筛选;改 view_state 即时无 confirm)。
   *  原住 ToolRowZone,统一口径后归筛选区(辅助查看 = 筛选 + 排序 + 搜索)。 */
  search?: { query: string; onQueryChange: (q: string) => void; placeholderKey?: string }
  /** 可选排序下拉(纯视图态;不 dispatch、不写领域)。 */
  sort?: { value: string; options: CardSort[]; onChange: (id: string) => void; labelKey?: string }
  /** 可选缩略图尺寸无级滑杆(纯视图偏好;不 dispatch、不写领域)。 */
  thumbSize?: { value: number; min: number; max: number; step?: number | 'any'; onChange: (value: number) => void; labelKey?: string }
}

function sortLabel(sort: CardSort, t: (key: string) => string): string {
  if (sort.labelKey) return t(sort.labelKey)
  return sort.label ?? sort.id
}

/* 折叠判据:测量层高度 > 2 行(chip 高 24×2 + 行 gap 4)+ 容差 6(边框/亚像素取整)。 */
const COLLAPSE_THRESHOLD_PX = 24 * 2 + 4 + 6

export function FilterBarZone({ filters, entities, pills, onPillChange, search, sort, thumbSize }: FilterBarZoneProps): ReactNode {
  const t = useT()
  const measureRef = useRef<HTMLDivElement>(null)
  const anchorRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [collapsed, setCollapsed] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)
  const [panelPos, setPanelPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 })

  // 溢出测量:观察隐藏测量层(真实 wrap、同 zone 宽、恒渲染)——高度越过 2 行阈值即折叠。
  // 测量层不随 collapsed 增删 → 无 set→重排→再 set 的反馈循环;容器变宽窄/值集增减都会
  // 改其高度并触发回调。jsdom 等无 ResizeObserver 环境跳过,保持内联态。
  // useLayoutEffect:首测在 paint 前完成,防"先画内联一帧再塌"的闪烁(同 useScrollAffordance 先例)。
  useLayoutEffect(() => {
    const meas = measureRef.current
    if (!meas || filters.length === 0 || typeof ResizeObserver === 'undefined') return
    const compute = () => { setCollapsed(meas.offsetHeight > COLLAPSE_THRESHOLD_PX) }
    compute()
    const ro = new ResizeObserver(compute)
    ro.observe(meas)
    return () => ro.disconnect()
  }, [filters, entities])

  // 折叠面板:外点 / Esc 关闭。
  useEffect(() => {
    if (!panelOpen) return
    const onDown = (e: MouseEvent) => {
      if (panelRef.current?.contains(e.target as Node) || anchorRef.current?.contains(e.target as Node)) return
      setPanelOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setPanelOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [panelOpen])

  // 打开后 measure → clamp 进视口(同 ContextMenu 模式:先锚定按钮左下,越界回收;下方不够翻上方)。
  useLayoutEffect(() => {
    if (!panelOpen) return
    const el = panelRef.current
    const a = anchorRef.current
    if (!el || !a) return
    const r = a.getBoundingClientRect()
    let left = r.left
    let top = r.bottom + 4
    const pr = el.getBoundingClientRect()
    if (left + pr.width > window.innerWidth) left = Math.max(4, window.innerWidth - pr.width - 4)
    if (top + pr.height > window.innerHeight) top = Math.max(4, r.top - pr.height - 4)
    setPanelPos({ left, top })
  }, [panelOpen])

  if (filters.length === 0 && !search && !sort?.options.length && !thumbSize) return null
  const placeholder = t(search?.placeholderKey ?? 'editor.search')
  const thumbSizeLabel = t(thumbSize?.labelKey ?? 'editor.thumbSize')
  const activeCount = filters.filter((f) => pills[f.id]).length

  // pill 组(内联 / 隐藏测量层 / 折叠面板三处同一结构):件 = .ze-chip,组 = __filtergroup(组名前缀 + wrap);
  // measure 态禁交互(tabIndex=-1,无 onClick),防 Tab 落进隐藏层。
  const renderFilterGroups = (mode: 'live' | 'measure') => filters.map((f) => (
    <span key={f.id} className="ze-editor__filtergroup ze-gap" data-gap="1">
      <span className="ze-text" data-role="secondary" data-size="xs" data-truncate="true">{t(f.labelKey)}</span>
      <button
        type="button" className="ze-chip" data-interactive="true"
        data-selected={pills[f.id] ? undefined : 'true'}
        tabIndex={mode === 'measure' ? -1 : undefined}
        onClick={mode === 'live' ? () => onPillChange(f.id, null) : undefined}
      >{t('editor.filterAll')}</button>
      {filterValues(f, entities).map((v) => (
        <button
          key={v} type="button" className="ze-chip" data-interactive="true"
          data-selected={pills[f.id] === v ? 'true' : undefined}
          tabIndex={mode === 'measure' ? -1 : undefined}
          onClick={mode === 'live' ? () => onPillChange(f.id, pills[f.id] === v ? null : v) : undefined}
        >{f.valueLabels?.[v] ? t(f.valueLabels[v]) : v}</button>
      ))}
    </span>
  ))

  return (
    <div
      className="ze-editor__zone"
      data-zone="manage" data-role="filterbar" data-function-zone="manage" data-layout="wrap-cluster"
      style={{ position: 'relative' }}
    >
      {search ? (
        <input
          className="ze-input ze-editor__search" data-size="sm" type="search"
          placeholder={placeholder} aria-label={placeholder}
          value={search.query} onChange={(e) => search.onQueryChange(e.target.value)}
        />
      ) : null}
      {/* 排序/滑杆 = 短组(label+单控件):.ze-row nowrap 不可拆;长值集 pill 组才用 filtergroup(内部 wrap)。 */}
      {sort?.options.length ? (
        <label className="ze-row" data-gap="1">
          <span className="ze-text" data-role="secondary" data-size="xs" data-truncate="true">{t(sort.labelKey ?? 'editor.sort')}</span>
          <select
            className="ze-input" data-size="sm"
            aria-label={t(sort.labelKey ?? 'editor.sort')}
            value={sort.value}
            onChange={(e) => sort.onChange(e.target.value)}
          >
            {sort.options.map((option) => (
              <option key={option.id} value={option.id}>{sortLabel(option, t)}</option>
            ))}
          </select>
        </label>
      ) : null}
      {thumbSize ? (
        <label className="ze-row" data-gap="1">
          <span className="ze-text" data-role="secondary" data-size="xs" data-truncate="true">{thumbSizeLabel}</span>
          <input
            className="ze-slider"
            type="range"
            min={thumbSize.min}
            max={thumbSize.max}
            step={thumbSize.step ?? 0.1}
            value={thumbSize.value}
            aria-label={thumbSizeLabel}
            onChange={(e) => thumbSize.onChange(Number(e.target.value))}
          />
        </label>
      ) : null}
      {filters.length > 0 && !collapsed ? renderFilterGroups('live') : null}
      {filters.length > 0 && collapsed ? (
        <button
          ref={anchorRef} type="button" className="ze-button" data-size="sm" data-variant="secondary"
          aria-haspopup="dialog" aria-expanded={panelOpen}
          onClick={() => setPanelOpen((o) => !o)}
        >
          {t('editor.filterPanel')}
          {activeCount > 0 ? <span className="ze-badge" data-size="sm" data-variant="accent">{activeCount}</span> : null}
        </button>
      ) : null}
      {/* 隐藏测量层:恒渲染完整 pill 组供折叠判据;不占布局、不进 Tab/读屏。 */}
      {filters.length > 0 ? (
        <div
          ref={measureRef} aria-hidden="true"
          style={{
            position: 'absolute', left: 0, right: 0, top: 0, zIndex: -1,
            visibility: 'hidden', pointerEvents: 'none',
            display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--ze-space-1)',
          }}
        >{renderFilterGroups('measure')}</div>
      ) : null}
      {panelOpen && collapsed ? createPortal(
        <div
          ref={panelRef} className="ze-popover" data-open="true" role="dialog"
          aria-label={t('editor.filterPanel')}
          style={{ position: 'fixed', left: panelPos.left, top: panelPos.top, maxWidth: 'min(420px, calc(100vw - 16px))' }}
        >
          <div className="ze-stack" data-gap="2">{renderFilterGroups('live')}</div>
        </div>,
        document.body,
      ) : null}
    </div>
  )
}
