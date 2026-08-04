/**
 * TaskEditor — `task` editor class:任务 / 批量进度看板(主区版的进度呈现)。
 *
 * 绑一个"任务型"entity_type,每个 entity = 一行任务,从 data 读 status / progress / stage。
 * 也可叠加 entity 级运行态(statusOf):running 显进度环、error 显错误占位 + 重试。
 * footer 的 pipeline 面板是全局聚合视图;TaskEditor 是把某批任务摆进主区的看板。
 *
 * zone 化(P3④):外壳=`EditorFrame`;身份行=`HeaderZone`;筛选段控=独立 filterbar 操作区
 * (局部 view 态、非 dispatch);内容=`ListZone`(单列任务行,
 * 自持行渲染/分阶段进度/运行态);footer=`StatusZone`(本槽 running/queued 切片)。
 * 筛选 view 态由本编辑器持有(段控在 toolbar、过滤后的 rows 喂给 ListZone)。
 *
 * 受控:entities 进,onRun/onCancel/dispatch 出。
 */

import { useMemo, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { FunctionZone } from '../../zones/FunctionZone'
import { ListZone } from '../../zones/ListZone'
import { StatusZone } from '../../zones/StatusZone'
import { KNOWN_STATUS, isKnownStatus, statusOfRow } from '../../lib/taskStages'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type TaskEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,与其它编辑器 header 一致)。 */
  editorName?: string
  /** 类型显示名(数据显示名,mod 经 schema.label 提供;无则回退裸 entityType)。 */
  typeLabel?: string
  entities: EntityDict[]
  selectedId?: string | null
  onSelect?: (id: string) => void
  statusOf?: (id: string) => EntityRunStatus | undefined
  /** 业务"运行"动作(mod 提供:dispatch 自家生成命令)*/
  onRun?: (id: string) => void
  /** 分阶段进度:entity.data 里存阶段数组的字段名(每项 {label/name, status?, progress?}) */
  stagesField?: string
  dispatch?: DispatchFn
}

export function TaskEditor({
  entityType, editorName, typeLabel, entities, selectedId, onSelect, statusOf, onRun, stagesField, dispatch,
}: TaskEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const typeName = typeLabel ?? entityType
  const [filter, setFilter] = useState<string>('all')

  const statusLabel = (s: string) => (isKnownStatus(s) ? t(`task.status.${s}`) : s)

  // 出现过的状态(供段控);固定顺序 + 未知状态附后
  const present = useMemo(() => {
    const set = new Set(entities.map((e) => statusOfRow(e, statusOf?.(e.id))))
    const ordered = KNOWN_STATUS.filter((s) => set.has(s))
    const extra = [...set].filter((s) => !isKnownStatus(s))
    return [...ordered, ...extra]
  }, [entities, statusOf])

  const rows = entities.filter((e) => filter === 'all' || statusOfRow(e, statusOf?.(e.id)) === filter)

  // 本槽 running/queued 计数(StatusZone)
  const running = entities.filter((e) => statusOfRow(e, statusOf?.(e.id)) === 'running').length
  const queued = entities.filter((e) => {
    const s = statusOfRow(e, statusOf?.(e.id))
    return s === 'queued' || s === 'pending'
  }).length

  const filterZone = present.length > 1 ? (
    <FunctionZone key="filter" id="filterbar" role="filterbar" kind="manage" layout="hug-scroll" ariaRole="group">
      <button type="button" className="ze-button" data-size="sm"
        data-variant={filter === 'all' ? 'primary' : 'ghost'}
        onClick={() => setFilter('all')}>{t('editor.filterAll')}</button>
      {present.map((s) => (
        <button key={s} type="button" className="ze-button" data-size="sm"
          data-variant={filter === s ? 'primary' : 'ghost'}
          onClick={() => setFilter(s)}>{statusLabel(s)}</button>
      ))}
    </FunctionZone>
  ) : null

  return (
    <EditorFrame
      editorClass="task"
      header={<HeaderZone title={editorName ?? typeName} count={filter === 'all' ? entities.length : `${rows.length}/${entities.length}`} />}
      zones={filterZone ? [filterZone] : undefined}
      statusbar={<StatusZone running={running} queued={queued} />}
    >
      <ListZone
        rows={rows} totalCount={entities.length} selectedId={selectedId} onSelect={onSelect}
        statusOf={statusOf} onRun={onRun} stagesField={stagesField} dispatch={fire}
      />
    </EditorFrame>
  )
}
