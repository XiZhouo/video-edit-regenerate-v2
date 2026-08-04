/**
 * ChartZone — 数据图表内容区(functional zone;SPEC §2B `chart`)
 *
 * 固定骨架(组件画死):量化摘要 KPI strip(count/sum/mean/min/max)+ `.ze-chart__plot` token 纯
 * SVG 画图(bar/line/area/scatter/histogram/pie)+ 悬停 tooltip/crosshair + 空态。薄 core + 零重依赖:
 * 海量点/交互缩放走懒模块 + renderers.chart(echarts 等),本轮不打包。复用现 `.ze-chart__*`(chart.css,L4)。
 *
 * 拓展点(受约束):字段映射(valField/xField/labelField,编辑器解析好传入)+ 闭集 `chartKind` +
 * `onSelect`(点选 entity)。悬停/光标态留本地 useState(view 态,不 dispatch)。
 */

import { useRef, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { EntityLoadingFrame } from '../lib/EntityLoadingFrame'
import { ZeIcon } from '../../lib/zeIcon'
import type { EntityDict } from '../types'

export type ChartKind = 'bar' | 'line' | 'area' | 'scatter' | 'histogram' | 'pie'

export type ChartZoneProps = {
  entities: EntityDict[]
  selectedId?: string | null
  onSelect?: (id: string | null) => void
  chartKind?: ChartKind
  /** 数值字段(编辑器按 yField/metricField/首个数值型解析好)。 */
  valField?: string
  xField?: string
  labelField?: string
}

const W = 320
const H = 170
const PAD = { l: 8, r: 8, t: 8, b: 8 }

function num(v: unknown): number | null {
  const n = typeof v === 'number' ? v : typeof v === 'string' ? Number(v) : NaN
  return Number.isFinite(n) ? n : null
}

function fmt(n: number): string {
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 })
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

type Stat = { count: number; sum: number; mean: number; min: number; max: number }
function summarize(values: number[]): Stat | null {
  if (!values.length) return null
  const sum = values.reduce((a, b) => a + b, 0)
  return { count: values.length, sum, mean: sum / values.length, min: Math.min(...values), max: Math.max(...values) }
}

/** 悬停态:命中元素 id + 展示文案 + 光标在 plot 容器内的像素坐标(用于 tooltip/crosshair 定位) */
type Hover = { id: string; label: string; value: string; px: number; py: number }
export type HoverFn = (id: string, label: string, value: string, e: React.MouseEvent) => void

export function ChartZone({
  entities, selectedId, onSelect, chartKind = 'bar', valField, xField, labelField,
}: ChartZoneProps): ReactNode {
  const t = useT()
  const plotRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<Hover | null>(null)

  const rows = entities
    .map((e) => ({ e, y: valField ? num(e.data[valField]) : null, x: xField ? num(e.data[xField]) : null }))
    .filter((r) => r.y !== null) as { e: EntityDict; y: number; x: number | null }[]

  const stat = summarize(rows.map((r) => r.y))
  const labelOf = (e: EntityDict) => String((labelField && e.data[labelField]) || e.name || e.id.slice(0, 6))

  const onHover: HoverFn = (id, label, value, e) => {
    const r = plotRef.current?.getBoundingClientRect()
    if (!r) return
    setHover({ id, label, value, px: e.clientX - r.left, py: e.clientY - r.top })
  }

  return (
    <>
      {/* 量化摘要 KPI strip */}
      {stat && (
        <div className="ze-chart__kpis ze-gap ze-pad ze-edge" data-gap="3" data-py="2" data-px="3" data-edge="bottom">
          {([['count', stat.count], ['sum', stat.sum], ['mean', stat.mean], ['min', stat.min], ['max', stat.max]] as [string, number][]).map(([k, v]) => (
            <div key={k} className="ze-status-tile">
              <span className="ze-status-tile__label">{t(`chart.${k}`)}</span>
              <span className="ze-status-tile__value">{fmt(v)}</span>
            </div>
          ))}
        </div>
      )}

      <EntityLoadingFrame minHeight={H} className="ze-chart__frame">
        {rows.length === 0 ? (
          <div className="ze-empty">
            <span className="ze-empty__icon" aria-hidden="true"><ZeIcon name="chart" size="lg" /></span>
            <span className="ze-empty__title">{t('chart.noData')}</span>
          </div>
        ) : (
          <div className="ze-chart__plot" ref={plotRef} onMouseLeave={() => setHover(null)}>
            <svg className="ze-chart__svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img">
              <ChartBody
                kind={chartKind} rows={rows} selectedId={selectedId ?? null}
                hoveredId={hover?.id ?? null} onPick={onSelect} onHover={onHover} labelOf={labelOf}
              />
            </svg>
            {hover && <div className="ze-chart__crosshair" style={{ left: hover.px }} aria-hidden="true" />}
            {hover && (
              <div className="ze-chart__tooltip" style={{ left: hover.px, top: hover.py }} role="status">
                <span className="ze-chart__tooltip-label">{hover.label}</span>
                <span className="ze-chart__tooltip-value">{hover.value}</span>
              </div>
            )}
          </div>
        )}
      </EntityLoadingFrame>
    </>
  )
}

type Row = { e: EntityDict; y: number; x: number | null }

function ChartBody({
  kind, rows, selectedId, hoveredId, onPick, onHover, labelOf,
}: {
  kind: ChartKind; rows: Row[]; selectedId: string | null; hoveredId: string | null
  onPick?: (id: string | null) => void; onHover: HoverFn; labelOf: (e: EntityDict) => string
}): ReactNode {
  const ys = rows.map((r) => r.y)
  const yMax = Math.max(...ys, 0)
  const yMin = Math.min(...ys, 0)
  const span = yMax - yMin || 1
  const innerW = W - PAD.l - PAD.r
  const innerH = H - PAD.t - PAD.b
  const yToPx = (y: number) => PAD.t + innerH - ((y - yMin) / span) * innerH

  // 基线(0 或 yMin)
  const baseY = yToPx(Math.max(0, yMin))
  const grid = <line className="ze-chart__axis" x1={PAD.l} y1={baseY} x2={W - PAD.r} y2={baseY} />

  if (kind === 'pie') {
    const total = rows.reduce((a, r) => a + Math.max(0, r.y), 0) || 1
    const cx = W / 2, cy = H / 2, rad = Math.min(innerW, innerH) / 2 - 2
    let a0 = -Math.PI / 2
    return (
      <>
        {rows.map((r, i) => {
          const frac = Math.max(0, r.y) / total
          const a1 = a0 + frac * Math.PI * 2
          const large = a1 - a0 > Math.PI ? 1 : 0
          const p = `M${cx},${cy} L${cx + rad * Math.cos(a0)},${cy + rad * Math.sin(a0)} A${rad},${rad} 0 ${large} 1 ${cx + rad * Math.cos(a1)},${cy + rad * Math.sin(a1)} Z`
          a0 = a1
          return <path key={r.e.id} className="ze-chart__slice" data-i={i % 6}
            data-selected={r.e.id === selectedId ? 'true' : undefined}
            data-hover={r.e.id === hoveredId ? 'true' : undefined} d={p}
            onClick={() => onPick?.(r.e.id)}
            onMouseEnter={(ev) => onHover(r.e.id, labelOf(r.e), fmt(r.y), ev)}
            onMouseMove={(ev) => onHover(r.e.id, labelOf(r.e), fmt(r.y), ev)} />
        })}
      </>
    )
  }

  if (kind === 'line' || kind === 'area') {
    const step = rows.length > 1 ? innerW / (rows.length - 1) : 0
    const pts = rows.map((r, i) => [PAD.l + i * step, yToPx(r.y)] as const)
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0]},${p[1]}`).join(' ')
    const areaD = `${d} L${pts[pts.length - 1][0]},${baseY} L${pts[0][0]},${baseY} Z`
    return (
      <>
        {grid}
        {kind === 'area' && <path className="ze-chart__area" d={areaD} />}
        <path className="ze-chart__line" d={d} />
        {pts.map((p, i) => (
          <circle key={rows[i].e.id} className="ze-chart__dot"
            data-selected={rows[i].e.id === selectedId ? 'true' : undefined}
            data-hover={rows[i].e.id === hoveredId ? 'true' : undefined}
            cx={p[0]} cy={p[1]} r={2.5} onClick={() => onPick?.(rows[i].e.id)}
            onMouseEnter={(ev) => onHover(rows[i].e.id, labelOf(rows[i].e), fmt(rows[i].y), ev)}
            onMouseMove={(ev) => onHover(rows[i].e.id, labelOf(rows[i].e), fmt(rows[i].y), ev)} />
        ))}
      </>
    )
  }

  if (kind === 'scatter') {
    const xs = rows.map((r) => r.x ?? 0)
    const xMax = Math.max(...xs), xMin = Math.min(...xs), xSpan = xMax - xMin || 1
    const xToPx = (x: number) => PAD.l + ((x - xMin) / xSpan) * innerW
    return (
      <>
        {grid}
        {rows.map((r) => (
          <circle key={r.e.id} className="ze-chart__dot"
            data-selected={r.e.id === selectedId ? 'true' : undefined}
            data-hover={r.e.id === hoveredId ? 'true' : undefined}
            cx={xToPx(r.x ?? 0)} cy={yToPx(r.y)} r={3} onClick={() => onPick?.(r.e.id)}
            onMouseEnter={(ev) => onHover(r.e.id, labelOf(r.e), `(${fmt(r.x ?? 0)}, ${fmt(r.y)})`, ev)}
            onMouseMove={(ev) => onHover(r.e.id, labelOf(r.e), `(${fmt(r.x ?? 0)}, ${fmt(r.y)})`, ev)} />
        ))}
      </>
    )
  }

  // histogram:把 y 分桶
  if (kind === 'histogram') {
    const bins = Math.min(12, Math.max(4, Math.ceil(Math.sqrt(rows.length))))
    const lo = Math.min(...ys), hi = Math.max(...ys), w = (hi - lo) / bins || 1
    const counts = new Array(bins).fill(0)
    for (const y of ys) counts[Math.min(bins - 1, Math.floor((y - lo) / w))]++
    const cMax = Math.max(...counts, 1)
    const bw = innerW / bins
    return (
      <>
        {counts.map((c, i) => {
          const h = (c / cMax) * innerH
          const range = `[${fmt(lo + i * w)}, ${fmt(lo + (i + 1) * w)})`
          return <rect key={i} className="ze-chart__bar" data-hover={`__bin${i}` === hoveredId ? 'true' : undefined}
            x={PAD.l + i * bw + 1} y={PAD.t + innerH - h} width={Math.max(1, bw - 2)} height={h}
            onMouseEnter={(ev) => onHover(`__bin${i}`, range, String(c), ev)}
            onMouseMove={(ev) => onHover(`__bin${i}`, range, String(c), ev)} />
        })}
      </>
    )
  }

  // bar(默认):每实体一条
  const bw = innerW / rows.length
  return (
    <>
      {grid}
      {rows.map((r, i) => {
        const top = yToPx(Math.max(r.y, 0))
        const h = Math.abs(baseY - yToPx(r.y))
        return <rect key={r.e.id} className="ze-chart__bar"
          data-selected={r.e.id === selectedId ? 'true' : undefined}
          data-hover={r.e.id === hoveredId ? 'true' : undefined}
          x={PAD.l + i * bw + 1} y={top} width={Math.max(1, bw - 2)} height={Math.max(0, h)}
          onClick={() => onPick?.(r.e.id)}
          onMouseEnter={(ev) => onHover(r.e.id, labelOf(r.e), fmt(r.y), ev)}
          onMouseMove={(ev) => onHover(r.e.id, labelOf(r.e), fmt(r.y), ev)} />
      })}
    </>
  )
}
