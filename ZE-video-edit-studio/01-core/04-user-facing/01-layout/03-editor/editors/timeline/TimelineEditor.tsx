/**
 * TimelineEditor — `timeline` editor class
 *
 * 时序型:把一组 clip entity 按「轨道 × 时间」摆到时间线上。clip 是可独立选中的 entity,
 * 三个数值字段经 view_state 映射:track(轨道行)/ start(起始)/ duration(时长)。
 * 本质是「1D 时序版 canvas2d 空间编辑器」:复用 entity.* 命令链,自身只做时间↔像素变换。
 *
 * zone 化(P3⑤):外壳=`EditorFrame`(rootRef 承接快捷键);身份行=`HeaderZone`;
 * 缩放按钮=独立 toolbar 操作区(局部 view 态、非 dispatch);
 * 时间线主体=`TimelineZone`(自持 drag draft + playhead + pointerup CAS 提交)。缩放 view 态
 * (pxPerUnit)由本编辑器持有(toolbar 与 zone 共用);删除快捷键留本层(它持 rootRef)。
 */

import { useMemo, useRef, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { useEditorShortcuts } from '../../lib/useEditorShortcuts'
import { useScrollAffordance } from '../../lib/useScrollAffordance'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { FunctionZone } from '../../zones/FunctionZone'
import { TimelineZone, type TimelineFields } from '../../zones/TimelineZone'
import { ZeIcon } from '../../../lib/zeIcon'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type { TimelineFields }

export type TimelineEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,与其它编辑器 header 一致)。 */
  editorName?: string
  typeLabel?: string
  entities: EntityDict[]
  selectedId: string | null
  onSelect?: (id: string | null) => void
  fields: TimelineFields
  tracks?: number
  /** 轨道数据与 DOM 的显式容量上限；缺省 64，框架仍会应用硬上限。 */
  maxTracks?: number
  /** opt-in：按占用轨渲染并在末尾保留一条空轨；缺省 false，固定轨行为不变。 */
  dynamicTracks?: boolean
  /** opt-in：允许在空轨拖出时间范围创建片段；必须同时声明 addCommand，缺省 false。 */
  rangeCreate?: boolean
  /** 吸附栅格(单位):0.5=半单位(默认) / 1=整单位 / 1/fps=帧 / 0=关闭。 */
  snap?: number
  /** unit↔秒 换算率(unit=帧时即 fps);缺省 1。 */
  unitsPerSecond?: number
  /** C0 共享时间游标(秒);注入则播放头受控、跨面板与 media/预览联动。 */
  currentTime?: number
  /** scrub 写回共享时间游标(秒)。 */
  onCurrentTimeChange?: (seconds: number) => void
  addCommand?: string
  updateCommand?: string
  deleteCommand?: string
  dispatch?: DispatchFn
  statusOf?: (id: string) => EntityRunStatus | undefined
}

const ZOOM_MIN = 16
const ZOOM_MAX = 200
const DEFAULT_PX_PER_UNIT = 48
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

export function TimelineEditor({
  entityType, editorName, typeLabel, entities, selectedId, onSelect, fields, tracks, maxTracks,
  dynamicTracks = false, rangeCreate = false, snap,
  unitsPerSecond, currentTime, onCurrentTimeChange,
  addCommand, updateCommand, deleteCommand, dispatch, statusOf,
}: TimelineEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const rootRef = useRef<HTMLDivElement>(null)
  const actionsRef = useScrollAffordance('x')
  const [pxPerUnit, setPxPerUnit] = useState(DEFAULT_PX_PER_UNIT)

  // 删除:Del/Backspace 删选中片段(编辑器聚焦时才触发);CAS 软删,带 expected_version。
  const shortcuts = useMemo(() => {
    const del = () => {
      if (!selectedId) return
      const e = entities.find((x) => x.id === selectedId)
      if (e) fire(deleteCommand ?? 'entity.delete', {
        type: entityType, id: e.id, expected_version: e.version,
      })
    }
    return [{ key: 'Delete', handler: del }, { key: 'Backspace', handler: del }]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, entities, deleteCommand, entityType, fire])
  useEditorShortcuts(rootRef, shortcuts)

  // 旧槽在空数据时仍显示标准空态；只有明确 opt-in 且存在写命令时才展示可操作空轨。
  const canCreateFirstClip = Boolean(addCommand && (dynamicTracks || rangeCreate))
  if (entities.length === 0 && !canCreateFirstClip) {
    return (
      <EditorFrame editorClass="timeline" rootRef={rootRef}
        header={<HeaderZone title={editorName ?? typeLabel ?? entityType} />}>
        <EditorFrame.Empty>{t('editor.noTimeline')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const zoom = (factor: number) => setPxPerUnit((p) => clamp(Math.round(p * factor), ZOOM_MIN, ZOOM_MAX))

  const zoomZone = (
    <FunctionZone key="zoom" id="toolbar" role="toolbar" kind="tool" layout="hug-scroll">
      <div ref={actionsRef} className="ze-editor__zone-actions ze-scroll-fade-x">
        <button className="ze-button" data-variant="ghost" data-size="sm" title={t('editor.zoomOut')} aria-label={t('editor.zoomOut')} onClick={() => zoom(1 / 1.5)}><ZeIcon name="zoom-out" /></button>
        <button className="ze-button" data-variant="ghost" data-size="sm" title={t('editor.zoomIn')} aria-label={t('editor.zoomIn')} onClick={() => zoom(1.5)}><ZeIcon name="zoom-in" /></button>
      </div>
    </FunctionZone>
  )

  return (
    <EditorFrame
      editorClass="timeline"
      rootRef={rootRef}
      header={<HeaderZone title={editorName ?? typeLabel ?? entityType} count={entities.length} />}
      zones={[zoomZone]}
    >
      <TimelineZone
        entityType={entityType} entities={entities} selectedId={selectedId} onSelect={onSelect}
        fields={fields} tracks={tracks} maxTracks={maxTracks} dynamicTracks={dynamicTracks} rangeCreate={rangeCreate}
        pxPerUnit={pxPerUnit} snap={snap}
        unitsPerSecond={unitsPerSecond} currentTime={currentTime} onCurrentTimeChange={onCurrentTimeChange}
        addCommand={addCommand} updateCommand={updateCommand} dispatch={fire} statusOf={statusOf}
      />
    </EditorFrame>
  )
}
