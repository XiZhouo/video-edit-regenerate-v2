/**
 * GraphCanvasZone — 关系图画布内容区(functional zone;SPEC §2B `canvas`/graph 变体)
 *
 * 固定骨架(组件画死):`.ze-graph__canvas` 自由定位画布 + `.ze-graph__edges` SVG 连线层 +
 * 节点层。交互(SPEC §2.2/§2.4):选/拖动节点(entity.update x/y CAS,pointerup 提交)/
 * 双击空白 addCommand 新增。拖动中只更新本地 draft,松手才 commit(零中途写库)。
 * 不引第三方图库(力导向/复杂连线属重库,留 mod/lazyModule)。复用现 `.ze-graph__*`(graph.css,L4)。
 *
 * 拓展点(受约束):字段映射 `fields`(x/y/label/deps)+ 命令 id(add/update/delete via dispatch)。
 * 删除快捷键由编辑器持(它有 rootRef);本 zone 只管画布与节点拖动。
 */

import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../types'

export type GraphFields = { x: string; y: string; label?: string; deps?: string }

export type GraphCanvasZoneProps = {
  entityType: string
  entities: EntityDict[]
  selectedId: string | null
  onSelect?: (id: string | null) => void
  fields: GraphFields
  addCommand?: string
  updateCommand?: string
  dispatch: DispatchFn
  statusOf?: (id: string) => EntityRunStatus | undefined
}

const NODE_W = 120
const NODE_H = 40
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

type Drag = { id: string; version: number; startX: number; startY: number; origX: number; origY: number; x: number; y: number } | null

export function GraphCanvasZone({
  entityType, entities, selectedId, onSelect, fields, addCommand, updateCommand, dispatch, statusOf,
}: GraphCanvasZoneProps) {
  const canvasRef = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<Drag>(null)

  const num = (e: EntityDict, f: string, d = 0) => {
    const v = e.data[f]
    return typeof v === 'number' && Number.isFinite(v) ? v : d
  }
  const nx = (e: EntityDict) => (drag && drag.id === e.id ? drag.x : num(e, fields.x))
  const ny = (e: EntityDict) => (drag && drag.id === e.id ? drag.y : num(e, fields.y))

  useEffect(() => {
    if (!drag) return
    const onMove = (ev: PointerEvent) => {
      const x = clamp(drag.origX + (ev.clientX - drag.startX), 0, 4000)
      const y = clamp(drag.origY + (ev.clientY - drag.startY), 0, 4000)
      setDrag({ ...drag, x, y })
    }
    const onUp = () => {
      if (drag.x !== drag.origX || drag.y !== drag.origY) {
        dispatch(updateCommand ?? 'entity.update', {
          type: entityType, id: drag.id,
          patches: { [fields.x]: Math.round(drag.x), [fields.y]: Math.round(drag.y) },
          expected_version: drag.version,
        })
      }
      setDrag(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [drag, entityType, fields, updateCommand, dispatch])

  const byId = new Map(entities.map((e) => [e.id, e]))
  const depsOf = (e: EntityDict): string[] => {
    const d = fields.deps ? e.data[fields.deps] : undefined
    return Array.isArray(d) ? d.filter((x): x is string => typeof x === 'string') : []
  }

  const onNodeDown = (e: EntityDict, ev: ReactPointerEvent) => {
    ev.stopPropagation()
    onSelect?.(e.id)
    setDrag({ id: e.id, version: e.version, startX: ev.clientX, startY: ev.clientY, origX: num(e, fields.x), origY: num(e, fields.y), x: num(e, fields.x), y: num(e, fields.y) })
  }
  const onCanvasDoubleClick = (ev: ReactMouseEvent) => {
    if (!addCommand) return
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    const x = clamp(ev.clientX - rect.left - NODE_W / 2, 0, 4000)
    const y = clamp(ev.clientY - rect.top - NODE_H / 2, 0, 4000)
    dispatch(addCommand, { type: entityType, node: { x: Math.round(x), y: Math.round(y) } })
  }

  return (
    <div ref={canvasRef} className="ze-graph__canvas ze-dotgrid" onDoubleClick={onCanvasDoubleClick}>
      {/* svg 贴 .ze-text[data-role=tertiary] 只为给 color=ink-tertiary,连线 stroke:currentColor 继承之 */}
      <svg className="ze-graph__edges ze-text" data-role="tertiary" aria-hidden="true">
        {entities.flatMap((e) =>
          depsOf(e).map((tid) => {
            const target = byId.get(tid)
            if (!target) return null
            return (
              <line
                key={`${e.id}->${tid}`} className="ze-graph__edge"
                x1={nx(e) + NODE_W / 2} y1={ny(e) + NODE_H / 2}
                x2={nx(target) + NODE_W / 2} y2={ny(target) + NODE_H / 2}
              />
            )
          }),
        )}
      </svg>
      {entities.map((e) => {
        const phase = statusOf?.(e.id)?.phase
        const label = (fields.label && typeof e.data[fields.label] === 'string' ? (e.data[fields.label] as string) : '') || e.name || e.id.slice(0, 6)
        return (
          <div
            key={e.id}
            className="ze-graph__node ze-pad ze-edge ze-round ze-surface ze-text ze-elevate ze-selectable ze-runstate"
            data-px="2" data-edge="all" data-tone="strong" data-radius="md" data-surface="raised" data-elev="sm"
            data-selected={e.id === selectedId ? 'true' : undefined}
            data-run-phase={phase}
            style={{ left: `${nx(e)}px`, top: `${ny(e)}px`, width: `${NODE_W}px`, height: `${NODE_H}px` }}
            onPointerDown={(ev) => onNodeDown(e, ev)}
          >
            <span className="ze-graph__node-label">{label}</span>
          </div>
        )
      })}
    </div>
  )
}
