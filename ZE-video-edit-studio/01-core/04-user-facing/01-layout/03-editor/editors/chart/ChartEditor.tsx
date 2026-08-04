/**
 * ChartEditor — `chart` editor class:数据图表 / 量化(多实体聚合)。
 *
 * 服务数据/量化型 skill(Data Query / Pipeline Graph / Word Cloud / Level Audit metrics / Trend Digest)。
 * 薄 core + 零重依赖:core 内置 token 纯 SVG 画常用图(bar/line/area/scatter/histogram/pie)+ 顶部量化
 * 摘要 KPI(count/sum/mean/min/max)。海量点/交互缩放走懒模块 + renderers.chart(echarts 等),本轮不打包。
 *
 * zone 化(P3⑤):外壳=`EditorFrame`;身份行=`HeaderZone`(title 带数值字段后缀 + count);
 * KPI strip + 绘图 + 悬停 tooltip/crosshair=`ChartZone`(自持 hover view 态)。
 * 数值字段(valField)在本编辑器解析(toolbar 标题需要),连同 entities/字段映射喂给 ChartZone。
 *
 * 受控:entities 进,onSelect 出。view_state 声明字段映射:{ chartKind, xField, yField, labelField, metricField }。
 */

import { type ReactNode } from 'react'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ChartZone, type ChartKind } from '../../zones/ChartZone'
import type { EntityDict, StatusLookup } from '../../types'

export type { ChartKind }

export type ChartEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,与其它编辑器 header 一致)。 */
  editorName?: string
  /** 类型显示名(数据显示名,mod 经 schema.label 提供;无则回退裸 entityType)。 */
  typeLabel?: string
  entities: EntityDict[]
  selectedId?: string | null
  onSelect?: (id: string | null) => void
  chartKind?: ChartKind
  xField?: string
  yField?: string
  labelField?: string
  metricField?: string
  statusOf?: StatusLookup
}

function num(v: unknown): number | null {
  const n = typeof v === 'number' ? v : typeof v === 'string' ? Number(v) : NaN
  return Number.isFinite(n) ? n : null
}

export function ChartEditor({
  entityType, editorName, typeLabel, entities, selectedId, onSelect,
  chartKind = 'bar', xField, yField, labelField, metricField,
}: ChartEditorProps): ReactNode {
  const typeName = typeLabel ?? entityType
  const live = entities.filter((e) => !e.deleted)

  // 数值字段:优先 yField/metricField,否则首个数值型字段(toolbar 标题与 ChartZone 共用)
  const valField = yField ?? metricField ?? (() => {
    const e = live[0]
    if (!e) return undefined
    return Object.keys(e.data).find((k) => num(e.data[k]) !== null)
  })()

  return (
    <EditorFrame editorClass="chart"
      header={<HeaderZone title={editorName ?? (valField ? `${typeName} · ${valField}` : typeName)} count={live.length} />}>
      <ChartZone
        entities={live} selectedId={selectedId} onSelect={onSelect}
        chartKind={chartKind} valField={valField} xField={xField} labelField={labelField}
      />
    </EditorFrame>
  )
}
