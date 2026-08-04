/**
 * FooterShell — Footer 容器 + Context Provider + 折叠/展开/拖拽状态机
 *
 * SPEC: ./SPEC.md
 *
 * 本组件:
 *   1. 提供 .ze-footer 根容器(折叠/展开两态)
 *   2. 装 CollapsedBar(折叠态)+ ExpandedPanel(展开主体)
 *   3. 把手=纯 toggle(click/双击);缝手柄 __resizer=拖拽改高(同 split divider 语言)
 *   4. 用 FooterDispatchContext.Provider 注入 dispatch
 *   5. import Footer CSS(整个 Footer 视觉由本文件触发加载)
 *
 * 全部 default fallback:`<FooterShell />` 无 props 也跑 = "模板态"
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { CollapsedBar } from './CollapsedBar'
import { DetailsTabs } from './DetailsTabs'
import { OpLogPanel } from './OpLogPanel'
import { PipelineList } from './PipelineList'
import { FooterDispatchContext } from './lib/dispatch'
import type { FooterProps } from './types'

// CSS 入口:整个 Footer 视觉由本文件触发加载
import '../../../../03-css-rules/05-compositions/status/footer/code.css'


const DEFAULT_EXPANDED_HEIGHT = 240
const MIN_EXPANDED_HEIGHT = 24    // CSS 同步,< 此值 = 自动 toggle 到折叠
const COLLAPSED_HEIGHT = 24


/** Hover/cursor drag 阈值:鼠标移动超过此距离才算"拖动",否则视为 click */
const DRAG_MOVE_THRESHOLD_PX = 3

/** 留给 Header + 中区最小可用高度的安全距离 */
const FOOTER_MAX_MARGIN_PX = 100


export function FooterShell(props: FooterProps): ReactNode {
  const { config, overrides, onExpandedChange, onExpandedHeightChange } = props
  const t = useT()

  // 产品名与 HeaderShell 同链:mod.product.name(i18n)有译值优先,缺则回退 skill_name 常量——
  // 否则语言切换时 Header 已译、Footer 仍英文常量,同屏中英各半。
  const productName = t('mod.product.name')
  const resolvedSkillName = productName && productName !== 'mod.product.name'
    ? productName
    : (config?.skill_name ?? t('footer.untitledSkill'))
  const skillName =
    overrides?.hide?.includes('skill_name')
      ? ''
      : resolvedSkillName

  const oplogMax = config?.oplog_max ?? 30
  const defaultExpandedHeight =
    config?.expanded_height_default ?? DEFAULT_EXPANDED_HEIGHT

  // ------------------------------------------------------------
  // 数据 fallback(模板态)
  // ------------------------------------------------------------
  const server = props.server ?? 'online'
  const sync = props.sync ?? { kind: 'unavailable' as const }
  const persistence = props.persistence ?? { kind: 'unavailable' as const }
  const errorCount = props.errorCount ?? 0
  const oplog = props.oplog ?? []
  const pipelines = props.pipelines ?? []
  const errors = props.errors ?? []
  const traceLink = props.traceLink ?? null
  const traceIdShort = props.traceIdShort

  // ------------------------------------------------------------
  // 展开 / 折叠状态(受控 vs 非受控)
  //
  // 注意:setExpanded / setExpandedHeight 通过 onExpandedChange /
  // onExpandedHeightChange 作为闭包依赖(不是 props 整体),避免每次 render
  // 重建 callback 链 → 拖拽 useEffect 反复 add/remove listener。
  // ------------------------------------------------------------
  const isExpandedControlled = props.expanded !== undefined
  const [internalExpanded, setInternalExpanded] = useState(false)
  const expanded = isExpandedControlled
    ? (props.expanded as boolean)
    : internalExpanded

  const setExpanded = useCallback(
    (v: boolean) => {
      if (!isExpandedControlled) setInternalExpanded(v)
      onExpandedChange?.(v)
    },
    [isExpandedControlled, onExpandedChange],
  )

  const isHeightControlled = props.expandedHeight !== undefined
  const [internalHeight, setInternalHeight] = useState(defaultExpandedHeight)
  const expandedHeight = isHeightControlled
    ? (props.expandedHeight as number)
    : internalHeight

  const setExpandedHeight = useCallback(
    (h: number) => {
      if (!isHeightControlled) setInternalHeight(h)
      onExpandedHeightChange?.(h)
    },
    [isHeightControlled, onExpandedHeightChange],
  )

  // 展开主体:三段标签切换(一次只显一段)。默认 oplog。
  const [activeSection, setActiveSection] =
    useState<'oplog' | 'pipelines' | 'details'>('oplog')

  // ------------------------------------------------------------
  // 拖拽逻辑
  //
  // 设计:
  //   - 折叠态 handle / 展开态 resizer 共用一套 onMouseDown
  //   - draggingRef 是真值源(同步,无 closure 问题);dragging state 仅给 CSS
  //   - "click vs drag" 用 movedRef 判定:鼠标移动 > 阈值 才算拖动
  //     ↓
  //   - click 事件总会跟在 mousedown+mouseup 之后(浏览器默认),
  //     onClickHandle 用 movedRef 判定:没拖过 → toggle;拖过 → 静默
  //   - 自动展开:用 expandedDuringDragRef 保证一次 drag 内只 setExpanded(true) 一次
  // ------------------------------------------------------------

  type DragState = {
    startY: number
    startHeight: number
    expandedAtStart: boolean
    moved: boolean
    autoExpanded: boolean
  }
  const draggingRef = useRef<DragState | null>(null)
  /** 上一次 mousedown→mouseup 期间是否拖动过(给 onClick 用) */
  const lastDragMovedRef = useRef(false)
  const [dragging, setDragging] = useState(false)

  const onMouseDownHandle = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      draggingRef.current = {
        startY: e.clientY,
        startHeight: expanded ? expandedHeight : COLLAPSED_HEIGHT,
        expandedAtStart: expanded,
        moved: false,
        autoExpanded: false,
      }
      setDragging(true)
    },
    [expanded, expandedHeight],
  )

  useEffect(() => {
    if (!dragging) return

    const onMove = (e: MouseEvent) => {
      const drag = draggingRef.current
      if (!drag) return
      const delta = drag.startY - e.clientY  // 向上为正 = 增高

      if (!drag.moved && Math.abs(delta) < DRAG_MOVE_THRESHOLD_PX) {
        return  // 未越过阈值,视为静止
      }
      drag.moved = true

      const targetHeight = Math.max(
        COLLAPSED_HEIGHT,
        Math.min(
          window.innerHeight - FOOTER_MAX_MARGIN_PX,
          drag.startHeight + delta,
        ),
      )

      if (targetHeight <= COLLAPSED_HEIGHT + 4) {
        // 拖到底:折叠
        if (drag.autoExpanded || drag.expandedAtStart) setExpanded(false)
      } else {
        // 一次 drag 内最多 setExpanded(true) 一次
        if (!drag.expandedAtStart && !drag.autoExpanded) {
          setExpanded(true)
          drag.autoExpanded = true
        }
        setExpandedHeight(targetHeight)
      }
    }

    const onUp = () => {
      // 先记录 moved 给 onClick 用(后于本次 mouseup 触发),再清 ref
      lastDragMovedRef.current = draggingRef.current?.moved ?? false
      draggingRef.current = null
      setDragging(false)
    }

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    // 注:不依赖 expanded / expandedHeight — draggingRef 闭包冻结的 startHeight 才是真值源
  }, [dragging, setExpanded, setExpandedHeight])

  // 把手 click(非拖拽)= toggle
  // 浏览器派发顺序:mousedown → mouseup → click。onUp 已把 moved 标记
  // 存到 lastDragMovedRef 后才清空 draggingRef,所以这里 ref 是同步真值。
  const onClickHandle = useCallback(() => {
    if (lastDragMovedRef.current) {
      lastDragMovedRef.current = false  // 消费完置回 false
      return
    }
    setExpanded(!expanded)
  }, [expanded, setExpanded])

  // 双击把手 = toggle(SPEC §2.5)
  const onDoubleClickHandle = useCallback(() => {
    setExpanded(!expanded)
  }, [expanded, setExpanded])

  // ------------------------------------------------------------
  // 衍生数据
  // ------------------------------------------------------------
  const activePipelines = pipelines.filter(
    (p) => p.status === 'running' || p.status === 'paused',
  )
  const topProgress = activePipelines[0]?.progress

  // ------------------------------------------------------------
  // 展开主体的标签:三段(最近操作 / 流水线 / 详情)合成 tab,一次只显一段
  // ------------------------------------------------------------
  const sections = useMemo<Array<{
    id: 'oplog' | 'pipelines' | 'details'
    label: string
    count?: number
    variant?: 'danger'
    render: () => ReactNode
  }>>(() => [
    {
      id: 'oplog',
      label: t('footer.recentOps'),
      count: oplog.length,
      render: () => (
        <OpLogPanel
          entries={oplog}
          max={oplogMax}
          canUndo={props.canUndo}
          canRedo={props.canRedo}
          onUndo={props.onUndo}
          onRedo={props.onRedo}
          hideTitle
        />
      ),
    },
    {
      id: 'pipelines',
      label: t('footer.pipelines'),
      count: activePipelines.length || undefined,
      render: () => (
        <PipelineList
          pipelines={pipelines}
          controlsAppend={overrides?.pipeline_controls_append}
          hideTitle
        />
      ),
    },
    {
      id: 'details',
      label: t('footer.tabDetails'),
      count: errors.length || undefined,
      variant: errors.length > 0 ? 'danger' : undefined,
      render: () => (
        <DetailsTabs
          sync={sync}
          errors={errors}
          traceLink={traceLink}
          tabsAppend={overrides?.details_tabs_append}
        />
      ),
    },
  ], [
    t, oplog, oplogMax, activePipelines, pipelines, errors, sync, traceLink, overrides,
    props.canUndo, props.canRedo, props.onUndo, props.onRedo,
  ])
  const activeSec = sections.find((s) => s.id === activeSection) ?? sections[0]

  // ------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------

  const content = (
    <>
      {/* [统一手柄语言] 缝手柄:footer 的前置兄弟(缝即元素,同 .ze-split__divider 的
          flex 项模型),负 margin 叠在中区底 padding 的空气缝上、净占位 0。
          不能做 .ze-footer 的子元素:footer 的 overflow:clip 会把悬出盒外的部分
          连绘制带命中一起裁掉(elementFromPoint 实测),真实鼠标永远抓不到缝。 */}
      <div
        className="ze-footer__resizer"
        data-dragging={dragging ? 'true' : 'false'}
        role="separator"
        aria-orientation="horizontal"
        onMouseDown={onMouseDownHandle}
      />
      <footer
        className="ze-footer"
        data-ze-region="footer"
        data-expanded={expanded ? 'true' : 'false'}
        style={
          expanded
            ? { height: `${Math.max(expandedHeight, MIN_EXPANDED_HEIGHT)}px` }
            : undefined
        }
        role="contentinfo"
      >
      {/* 把手:纯 toggle 按钮(click/双击);拉伸职责已移到上方缝手柄 */}
      <div
        className="ze-footer__handle"
        role="button"
        aria-label={expanded ? t('footer.collapse') : t('footer.expand')}
        aria-expanded={expanded}
        tabIndex={0}
        onClick={onClickHandle}
        onDoubleClick={onDoubleClickHandle}
      >
        {overrides?.collapsed_slot ?? (
          <CollapsedBar
            serverStatus={server}
            skillName={skillName}
            sync={sync}
            persistence={persistence}
            errorCount={errorCount}
            latestOp={oplog[0]}
            traceIdShort={traceIdShort}
            activePipelineCount={activePipelines.length}
            topPipelineProgress={
              typeof topProgress === 'number' ? topProgress : undefined
            }
            hideServer={overrides?.hide?.includes('server')}
            hideSkillName={overrides?.hide?.includes('skill_name')}
            hideSync={overrides?.hide?.includes('sync')}
            hideProgress={overrides?.hide?.includes('progress')}
            hideErrors={overrides?.hide?.includes('errors')}
            hideTraceId={overrides?.hide?.includes('trace_id')}
            segmentAppend={overrides?.segment_append}
          />
        )}
      </div>

      {/* 展开主体:标签切换,一次只显一段(解决多段同时堆叠看不过来)*/}
      {expanded && (
        <div className="ze-footer__expanded">
          <div className="ze-tabs" data-density="compact">
            <header className="ze-tabs__headers" role="tablist">
              {sections.map((s) => (
                <button
                  key={s.id}
                  id={`ze-footer-tab-${s.id}`}
                  type="button"
                  role="tab"
                  className="ze-tab"
                  data-active={s.id === activeSec.id ? 'true' : 'false'}
                  data-variant={s.variant ?? undefined}
                  aria-selected={s.id === activeSec.id}
                  aria-controls="ze-footer-tabpanel"
                  onClick={() => setActiveSection(s.id)}
                >
                  <span>{s.label}</span>
                  {typeof s.count === 'number' && s.count > 0 && (
                    <span className="ze-tab__count">{s.count}</span>
                  )}
                </button>
              ))}
            </header>
            <div
              className="ze-tabs__body"
              role="tabpanel"
              id="ze-footer-tabpanel"
              aria-labelledby={`ze-footer-tab-${activeSec.id}`}
            >
              {activeSec.render()}
            </div>
          </div>
        </div>
      )}
      </footer>
    </>
  )

  if (props.dispatch) {
    return (
      <FooterDispatchContext.Provider value={props.dispatch}>
        {content}
      </FooterDispatchContext.Provider>
    )
  }
  return content
}
