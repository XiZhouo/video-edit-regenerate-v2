/**
 * TimelineZone — 时间线内容区(functional zone;SPEC §2B `timeline`)
 *
 * 固定骨架(组件画死):`.ze-timeline__scroll` > `__inner`(总宽)+ 时间刻度 ruler + 轨道 lanes +
 * 片段 clip + 播放头。把一组 clip entity 按「轨道 × 时间」摆上时间线。交互(SPEC §2.2):选 /
 * 水平拖动改 start(可跨轨)/ 拖右手柄改 duration / 双击空轨 addCommand / 可选拖范围新增 /
 * 拖 ruler 移播放头。
 * 写走 command(SPEC §2.4):拖动中只更新本地 draft,pointerup 才 commit entity.update {patches,
 * expected_version}(同 FormZone 草稿→CAS 模式,零中途写库)。复用现 `.ze-timeline__*`(timeline.css,L4)。
 *
 * 播放头(C0 共享时间游标):装配层注入 `onCurrentTimeChange` 时,播放头**受控** —— 位置 =
 * `currentTime(秒) × unitsPerSecond`,scrub 写回共享 `currentTime`(跨面板与 media/预览联动,不入 undo);
 * 未注入则退回本地 view-only 态(零行为变化)。换算纯函数见 `lib/playhead`。
 *
 * 拓展点(受约束):字段映射 `fields`(track/start/duration/label/color)+ 命令 id(add/update via dispatch)+
 * `tracks` 轨道数 + `pxPerUnit` 缩放(编辑器持有,zoom 段控在 toolbar)+ `unitsPerSecond`(unit↔秒)。
 * 删除快捷键由编辑器持(它有 rootRef)。
 */

import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { secondsToUnits, unitsToSeconds } from '../lib/playhead'
import {
  MIN_DURATION, DEFAULT_SNAP, clampNum, snapUnits, unitsFromX, trackFromY,
  moveStart, resizeDuration, trimStart, createRange, laneCount, normalizeMaxTracks, normalizeTrack,
} from '../lib/timelineModel'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../types'

export type TimelineFields = {
  track: string
  start: string
  duration: string
  label?: string
  color?: string
}

export type TimelineZoneProps = {
  entityType: string
  entities: EntityDict[]
  selectedId: string | null
  onSelect?: (id: string | null) => void
  fields: TimelineFields
  tracks?: number
  /** 轨道 DOM/交互容量；缺省 64，并有框架硬上限。 */
  maxTracks?: number
  /** opt-in：按占用轨渲染并在末尾保留一条空轨；缺省 false。 */
  dynamicTracks?: boolean
  /** opt-in：空轨 pointer drag 创建范围；仅与 addCommand 同时存在时启用，缺省 false。 */
  rangeCreate?: boolean
  /** 缩放(像素/单位;编辑器持有,zoom 按钮在 toolbar 改它)。 */
  pxPerUnit: number
  /** 吸附栅格(单位):0.5=半单位(默认) / 1=整单位 / 1/fps=帧 / 0=关闭。view_state `snap`。 */
  snap?: number
  /** unit↔秒 换算率(unit=帧时即 fps,unit=秒时即 1);缺省 1。共享 currentTime 规范单位=秒。 */
  unitsPerSecond?: number
  /** C0 共享时间游标(秒);注入则播放头受控。 */
  currentTime?: number
  /** scrub 写回共享时间游标(秒);注入则播放头受控,否则退回本地 view-only 态。 */
  onCurrentTimeChange?: (seconds: number) => void
  addCommand?: string
  updateCommand?: string
  dispatch: DispatchFn
  statusOf?: (id: string) => EntityRunStatus | undefined
}

function num(e: EntityDict, field: string, dflt = 0): number {
  const v = e.data[field]
  return typeof v === 'number' && Number.isFinite(v) ? v : dflt
}

type Drag =
  | { mode: 'move'; id: string; version: number; startX: number; origStart: number; track: number; start: number }
  | { mode: 'resize'; id: string; version: number; startX: number; origDuration: number; duration: number }
  | { mode: 'trim-start'; id: string; version: number; startX: number; origStart: number; origDuration: number; start: number; duration: number }
  | { mode: 'create'; track: number; anchor: number; start: number; duration: number; valid: boolean }
  | { mode: 'scrub' }
  | null

export function TimelineZone({
  entityType, entities, selectedId, onSelect, fields, tracks, maxTracks,
  dynamicTracks = false, rangeCreate = false, pxPerUnit, snap,
  unitsPerSecond, currentTime, onCurrentTimeChange,
  addCommand, updateCommand, dispatch, statusOf,
}: TimelineZoneProps) {
  const innerRef = useRef<HTMLDivElement>(null)
  const lanesRef = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<Drag>(null)
  const snapG = typeof snap === 'number' ? snap : DEFAULT_SNAP   // 吸附栅格(可配,缺省半单位)
  // 播放头:注入 onCurrentTimeChange → 受控(读共享 currentTime、scrub 写回共享轴,跨面板联动、不入 undo);
  // 未注入 → 本地 view-only 态(零行为变化)。unit↔秒 换算见 lib/playhead。
  const [localPlayhead, setLocalPlayhead] = useState(0)
  const playhead = onCurrentTimeChange ? Math.max(0, secondsToUnits(currentTime ?? 0, unitsPerSecond)) : localPlayhead
  const scrubToUnits = (units: number) => {
    if (onCurrentTimeChange) onCurrentTimeChange(unitsToSeconds(units, unitsPerSecond))
    else setLocalPlayhead(units)
  }

  // 所有数据、固定轨与动态尾轨共用同一有限容量；极端脏轨号只会收敛到末轨，不会消失或制造无界 DOM。
  const safeMaxTracks = normalizeMaxTracks(maxTracks)
  const trackCount = laneCount(entities.map((e) => e.data[fields.track]), {
    dynamic: dynamicTracks, fixed: tracks, maxTracks: safeMaxTracks,
  })
  // 时间总长:数据末端 + 余量,保证有空白可双击新增。
  const dataEnd = entities.reduce((m, e) => Math.max(m, num(e, fields.start) + num(e, fields.duration, MIN_DURATION)), 0)
  const totalUnits = Math.ceil(Math.max(8, dataEnd, playhead)) + 2
  const contentW = totalUnits * pxPerUnit

  // 拖动期间在 window 上听 move/up(超出元素也跟手);drag 变化时重挂。
  useEffect(() => {
    if (!drag) return
    const trackAtY = (clientY: number): number => {
      const rect = lanesRef.current?.getBoundingClientRect()
      if (!rect) return 0
      return trackFromY(clientY, rect.top, rect.height, trackCount)
    }
    const onMove = (ev: PointerEvent) => {
      if (drag.mode === 'move') {
        const du = (ev.clientX - drag.startX) / pxPerUnit
        setDrag({ ...drag, start: moveStart(drag.origStart, du, totalUnits, snapG), track: trackAtY(ev.clientY) })
      } else if (drag.mode === 'resize') {
        const du = (ev.clientX - drag.startX) / pxPerUnit
        setDrag({ ...drag, duration: resizeDuration(drag.origDuration, du, snapG) })
      } else if (drag.mode === 'trim-start') {
        const du = (ev.clientX - drag.startX) / pxPerUnit
        setDrag({ ...drag, ...trimStart(drag.origStart, drag.origDuration, du, snapG) })
      } else if (drag.mode === 'scrub') {
        const left = innerRef.current?.getBoundingClientRect().left ?? 0
        const units = clampNum(snapUnits(unitsFromX(ev.clientX, left, pxPerUnit), snapG), 0, totalUnits)
        if (onCurrentTimeChange) onCurrentTimeChange(unitsToSeconds(units, unitsPerSecond))
        else setLocalPlayhead(units)
      } else if (drag.mode === 'create') {
        const left = innerRef.current?.getBoundingClientRect().left ?? 0
        const current = unitsFromX(ev.clientX, left, pxPerUnit)
        setDrag({ ...drag, ...createRange(drag.anchor, current, totalUnits, snapG) })
      }
    }
    const onUp = () => {
      if (drag.mode === 'move') {
        const patches: Record<string, unknown> = {}
        if (drag.start !== drag.origStart) patches[fields.start] = drag.start
        const source = entities.find((e) => e.id === drag.id)
        if (source) {
          const rawTrack = source.data[fields.track]
          const origTrack = normalizeTrack(rawTrack, trackCount)
          // 拖动边界顺便把负数/小数/NaN/超上限的历史值写回为当前可见的规范整数轨号。
          if (drag.track !== origTrack || rawTrack !== drag.track) patches[fields.track] = drag.track
        }
        if (Object.keys(patches).length) {
          dispatch(updateCommand ?? 'entity.update', { type: entityType, id: drag.id, patches, expected_version: drag.version })
        }
      } else if (drag.mode === 'resize') {
        if (drag.duration !== drag.origDuration) {
          dispatch(updateCommand ?? 'entity.update', {
            type: entityType, id: drag.id,
            patches: { [fields.duration]: drag.duration }, expected_version: drag.version,
          })
        }
      } else if (drag.mode === 'trim-start') {
        // 左把手:右缘固定,start 与 duration 一并改 —— 一次 entity.update(单 Operation,原子撤销)。
        const patches: Record<string, unknown> = {}
        if (drag.start !== drag.origStart) patches[fields.start] = drag.start
        if (drag.duration !== drag.origDuration) patches[fields.duration] = drag.duration
        if (Object.keys(patches).length) {
          dispatch(updateCommand ?? 'entity.update', { type: entityType, id: drag.id, patches, expected_version: drag.version })
        }
      } else if (drag.mode === 'create') {
        // `valid` 只来自 clamp+snap 后的两端比较；最小时长补齐不能把边界点击/吸附抖动变成创建。
        if (rangeCreate && addCommand && drag.valid) {
          dispatch(addCommand, {
            type: entityType,
            clip: { track: normalizeTrack(drag.track, trackCount), start: drag.start, duration: drag.duration },
          })
        }
      }
      setDrag(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [drag, pxPerUnit, totalUnits, trackCount, entities, entityType, fields, updateCommand, addCommand,
    rangeCreate, dispatch, onCurrentTimeChange, unitsPerSecond, snapG])

  const onClipDown = (e: EntityDict, ev: ReactPointerEvent) => {
    ev.stopPropagation()
    onSelect?.(e.id)
    setDrag({
      mode: 'move', id: e.id, version: e.version, startX: ev.clientX,
      origStart: num(e, fields.start), start: num(e, fields.start), track: normalizeTrack(e.data[fields.track], trackCount),
    })
  }
  const onResizeDown = (e: EntityDict, ev: ReactPointerEvent) => {
    ev.stopPropagation()
    onSelect?.(e.id)
    setDrag({
      mode: 'resize', id: e.id, version: e.version, startX: ev.clientX,
      origDuration: num(e, fields.duration, MIN_DURATION), duration: num(e, fields.duration, MIN_DURATION),
    })
  }
  // 左把手:右缘 end 固定,改 start+duration(== segment 区间左缘 / 媒体 head trim 的通用形)。
  const onTrimStartDown = (e: EntityDict, ev: ReactPointerEvent) => {
    ev.stopPropagation()
    onSelect?.(e.id)
    const origStart = num(e, fields.start)
    const origDuration = num(e, fields.duration, MIN_DURATION)
    setDrag({
      mode: 'trim-start', id: e.id, version: e.version, startX: ev.clientX,
      origStart, origDuration, start: origStart, duration: origDuration,
    })
  }
  const onLaneDoubleClick = (laneIndex: number, ev: ReactMouseEvent) => {
    if (!addCommand) return
    const left = innerRef.current?.getBoundingClientRect().left ?? 0
    const start = clampNum(snapUnits(unitsFromX(ev.clientX, left, pxPerUnit), snapG), 0, totalUnits - MIN_DURATION)
    dispatch(addCommand, { type: entityType, clip: { track: normalizeTrack(laneIndex, trackCount), start, duration: 2 } })
  }
  const onLanePointerDown = (laneIndex: number, ev: ReactPointerEvent) => {
    if (!rangeCreate || !addCommand || ev.button !== 0) return
    const left = innerRef.current?.getBoundingClientRect().left ?? 0
    const anchor = clampNum(snapUnits(unitsFromX(ev.clientX, left, pxPerUnit), snapG), 0, totalUnits)
    onSelect?.(null)
    setDrag({ mode: 'create', track: normalizeTrack(laneIndex, trackCount), anchor, ...createRange(anchor, anchor, totalUnits, snapG) })
  }

  const liveStart = (e: EntityDict) =>
    (drag?.mode === 'move' || drag?.mode === 'trim-start') && drag.id === e.id ? drag.start : num(e, fields.start)
  const liveTrack = (e: EntityDict) => normalizeTrack(
    drag?.mode === 'move' && drag.id === e.id ? drag.track : e.data[fields.track],
    trackCount,
  )
  const liveDuration = (e: EntityDict) =>
    (drag?.mode === 'resize' || drag?.mode === 'trim-start') && drag.id === e.id ? drag.duration : num(e, fields.duration, MIN_DURATION)

  return (
    <div className="ze-timeline__scroll">
      <div ref={innerRef} className="ze-timeline__inner" style={{ width: `${contentW}px` }}>
        {/* 时间刻度 */}
        <div className="ze-timeline__ruler" onPointerDown={(ev) => { ev.preventDefault(); setDrag({ mode: 'scrub' }); const left = innerRef.current?.getBoundingClientRect().left ?? 0; scrubToUnits(clampNum(snapUnits(unitsFromX(ev.clientX, left, pxPerUnit), snapG), 0, totalUnits)) }}>
          {Array.from({ length: totalUnits + 1 }, (_, u) => (
            <span key={u} className="ze-timeline__tick" style={{ left: `${u * pxPerUnit}px` }}>{u}</span>
          ))}
        </div>
        {/* 轨道 + 片段 */}
        <div ref={lanesRef} className="ze-timeline__lanes">
          {Array.from({ length: trackCount }, (_, lane) => (
            <div key={lane} className="ze-timeline__lane"
              onPointerDown={(ev) => onLanePointerDown(lane, ev)}
              onDoubleClick={(ev) => onLaneDoubleClick(lane, ev)}>
              {entities.filter((e) => liveTrack(e) === lane).map((e) => {
                const phase = statusOf?.(e.id)?.phase
                const label = (fields.label && typeof e.data[fields.label] === 'string' ? e.data[fields.label] as string : '') || e.name || e.id.slice(0, 6)
                const color = fields.color && typeof e.data[fields.color] === 'string' ? e.data[fields.color] as string : undefined
                return (
                  <div
                    key={e.id}
                    className="ze-timeline__clip"
                    data-id={e.id}
                    data-selected={e.id === selectedId ? 'true' : undefined}
                    data-run-phase={phase}
                    style={{ left: `${liveStart(e) * pxPerUnit}px`, width: `${liveDuration(e) * pxPerUnit}px`, ...(color ? { ['--clip-color' as string]: color } : {}) }}
                    onPointerDown={(ev) => onClipDown(e, ev)}
                  >
                    <span className="ze-timeline__clip-handle ze-timeline__clip-handle--start" onPointerDown={(ev) => onTrimStartDown(e, ev)} />
                    <span className="ze-timeline__clip-label">{label}</span>
                    <span className="ze-timeline__clip-handle" onPointerDown={(ev) => onResizeDown(e, ev)} />
                  </div>
                )
              })}
              {drag?.mode === 'create' && drag.track === lane && drag.valid && (
                <div className="ze-timeline__clip" data-draft="true" aria-hidden="true"
                  style={{ left: `${drag.start * pxPerUnit}px`, width: `${drag.duration * pxPerUnit}px` }} />
              )}
            </div>
          ))}
          {/* 播放头(view-only) */}
          <div className="ze-timeline__playhead" style={{ left: `${playhead * pxPerUnit}px` }} />
        </div>
      </div>
    </div>
  )
}
