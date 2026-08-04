/**
 * CurveEditor — `keyframe-curve` editor class (status: candidate)
 *
 * 数值×时间曲线面:每个关键帧是可独立选中的 entity,view_state 映射 time(x)/value(y)/ease。
 * 与 timeline(片段编排)并列的第二个时序面 —— timeline 摆「块」,curve 画「值随时间变」。
 * 复用 timelineModel(time↔px、吸附)+ curveModel(value↔y、自适应值域)。与 media/timeline
 * 经共享 currentTime(秒)联动播放头(SPEC C0),互不 import。
 *
 * 候选(无真实 v3 消费者):标 status=candidate。消费者(skeletal-animation /
 * motion-spec-builder / controlled-image-generate)接 UI 后转 adopted + 补 fixture。
 */
import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { useEditorShortcuts } from '../../lib/useEditorShortcuts'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { secondsToUnits } from '../../lib/playhead'
import { DEFAULT_SNAP, clampNum, snapUnits, unitsFromX } from '../../lib/timelineModel'
import { autoValueRange, valueToY, yToValue, sortByTime, type ValueRange } from '../../lib/curveModel'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type CurveFields = { time: string; value: string; ease?: string }

export type CurveEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,与其它编辑器 header 一致)。 */
  editorName?: string
  typeLabel?: string
  entities: EntityDict[]
  selectedId: string | null
  onSelect?: (id: string | null) => void
  fields: CurveFields
  /** 值域(纵轴);缺省按数据自适应。 */
  valueMin?: number
  valueMax?: number
  /** 吸附栅格(时间,单位);缺省半单位、0 关闭。 */
  snap?: number
  /** unit↔秒 换算 + 共享时间游标(秒);注入则画竖直播放头。 */
  unitsPerSecond?: number
  currentTime?: number
  addCommand?: string
  updateCommand?: string
  deleteCommand?: string
  dispatch?: DispatchFn
  statusOf?: (id: string) => EntityRunStatus | undefined
}

const DEFAULT_PX_PER_UNIT = 64   // 固定时间缩放(v1 候选;缩放工具条待 L3 zone API 后补)
const POINT = 12   // 关键帧点边长(px;内联尺寸,04-editors CSS 保持零令牌)

function num(e: EntityDict, field: string, dflt = 0): number {
  const v = e.data[field]
  return typeof v === 'number' && Number.isFinite(v) ? v : dflt
}

type Drag = { id: string; version: number; time: number; value: number } | null

export function CurveEditor({
  entityType, editorName, typeLabel, entities, selectedId, onSelect, fields,
  valueMin, valueMax, snap, unitsPerSecond, currentTime,
  addCommand, updateCommand, deleteCommand, dispatch, statusOf,
}: CurveEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const rootRef = useRef<HTMLDivElement>(null)
  const planeRef = useRef<HTMLDivElement>(null)
  const pxPerUnit = DEFAULT_PX_PER_UNIT
  const [planeH, setPlaneH] = useState(0)
  const [drag, setDrag] = useState<Drag>(null)
  const snapG = typeof snap === 'number' ? snap : DEFAULT_SNAP

  // 删除选中关键帧(Del/Backspace,CAS)。
  const shortcuts = useMemo(() => {
    const del = () => {
      if (!selectedId || !deleteCommand) return
      const e = entities.find((x) => x.id === selectedId)
      if (e) fire(deleteCommand, { type: entityType, id: e.id, expected_version: e.version })
    }
    return [{ key: 'Delete', handler: del }, { key: 'Backspace', handler: del }]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, entities, deleteCommand, entityType, fire])
  useEditorShortcuts(rootRef, shortcuts)

  // 绘图区高度(value↔y 需真实高度;ResizeObserver 一帧后即真值)。
  useEffect(() => {
    const el = planeRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setPlaneH(el.clientHeight))
    ro.observe(el)
    setPlaneH(el.clientHeight)
    return () => ro.disconnect()
  }, [])

  // 值域取已提交数据(非 live),拖动期间稳定不抖轴。
  const range: ValueRange = (typeof valueMin === 'number' && typeof valueMax === 'number' && valueMax > valueMin)
    ? { min: valueMin, max: valueMax }
    : autoValueRange(entities.map((e) => num(e, fields.value)))

  const liveTime = (e: EntityDict) => (drag && drag.id === e.id ? drag.time : num(e, fields.time))
  const liveValue = (e: EntityDict) => (drag && drag.id === e.id ? drag.value : num(e, fields.value))

  const dataEnd = entities.reduce((m, e) => Math.max(m, liveTime(e)), 0)
  const totalUnits = Math.ceil(Math.max(8, dataEnd)) + 2
  const contentW = totalUnits * pxPerUnit
  const playheadUnits = currentTime !== undefined ? Math.max(0, secondsToUnits(currentTime, unitsPerSecond)) : null

  useEffect(() => {
    if (!drag) return
    const onMove = (ev: PointerEvent) => {
      const rect = planeRef.current?.getBoundingClientRect()
      if (!rect) return
      const time = clampNum(snapUnits(unitsFromX(ev.clientX, rect.left, pxPerUnit), snapG), 0, totalUnits)
      const value = yToValue(ev.clientY - rect.top, range, rect.height)
      setDrag((d) => (d ? { ...d, time, value } : d))
    }
    const onUp = () => {
      // 副作用(fire)放 setState 外:setState updater 必须纯,Strict Mode 双调会重发命令。
      // drag 是本 effect 闭包值(顶部 if (!drag) return 保证非空),读它安全。
      const e = entities.find((x) => x.id === drag.id)
      const patches: Record<string, unknown> = {}
      if (e && drag.time !== num(e, fields.time)) patches[fields.time] = drag.time
      if (e && drag.value !== num(e, fields.value)) patches[fields.value] = drag.value
      if (Object.keys(patches).length) {
        fire(updateCommand ?? 'entity.update', { type: entityType, id: drag.id, patches, expected_version: drag.version })
      }
      setDrag(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [drag, pxPerUnit, totalUnits, snapG, range, entities, entityType, fields, updateCommand, fire])

  const onPointDown = (e: EntityDict, ev: ReactPointerEvent) => {
    ev.stopPropagation()
    onSelect?.(e.id)
    setDrag({ id: e.id, version: e.version, time: num(e, fields.time), value: num(e, fields.value) })
  }
  const onPlaneDoubleClick = (ev: ReactMouseEvent) => {
    if (!addCommand) return
    const rect = planeRef.current?.getBoundingClientRect()
    if (!rect) return
    const time = clampNum(snapUnits(unitsFromX(ev.clientX, rect.left, pxPerUnit), snapG), 0, totalUnits)
    const value = yToValue(ev.clientY - rect.top, range, rect.height)
    fire(addCommand, { type: entityType, keyframe: { time, value } })
  }

  if (entities.length === 0) {
    return (
      <EditorFrame editorClass="keyframe-curve" rootRef={rootRef} header={<HeaderZone title={editorName ?? typeLabel ?? entityType} />}>
        <EditorFrame.Empty>{t('editor.noKeyframeCurve')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const sorted = sortByTime(entities, (e) => liveTime(e))
  const polyline = sorted.map((e) => `${liveTime(e) * pxPerUnit},${valueToY(liveValue(e), range, planeH)}`).join(' ')

  return (
    <EditorFrame editorClass="keyframe-curve" rootRef={rootRef}
      header={<HeaderZone title={editorName ?? typeLabel ?? entityType} count={entities.length} />}>
      <div className="ze-curve__scroll">
        <div ref={planeRef} className="ze-curve__plane ze-dotgrid" style={{ width: `${contentW}px` }} onDoubleClick={onPlaneDoubleClick}>
          {/* svg 贴 .ze-text[data-role=tertiary] 只为给 currentColor(折线 stroke 继承之) */}
          <svg className="ze-curve__edges ze-text" data-role="tertiary" aria-hidden="true">
            {planeH > 0 && <polyline className="ze-curve__line" points={polyline} fill="none" />}
            {playheadUnits !== null && (
              <line className="ze-curve__playhead" x1={playheadUnits * pxPerUnit} y1={0} x2={playheadUnits * pxPerUnit} y2={planeH} />
            )}
          </svg>
          {planeH > 0 && entities.map((e) => {
            const phase = statusOf?.(e.id)?.phase
            return (
              <div
                key={e.id}
                className="ze-curve__key ze-edge ze-round ze-surface ze-selectable ze-runstate"
                data-edge="all" data-tone="strong" data-radius="sm" data-surface="raised"
                data-selected={e.id === selectedId ? 'true' : undefined}
                data-run-phase={phase}
                style={{ left: `${liveTime(e) * pxPerUnit - POINT / 2}px`, top: `${valueToY(liveValue(e), range, planeH) - POINT / 2}px`, width: `${POINT}px`, height: `${POINT}px` }}
                onPointerDown={(ev) => onPointDown(e, ev)}
              />
            )
          })}
        </div>
      </div>
    </EditorFrame>
  )
}
