/**
 * Canvas2dSpatial — canvas2d 的交互式空间编辑分支(框架级)
 *
 * 把绑定类型的所有实体当 box 渲染,选中件出 8 把手 gizmo:移动/缩放/旋转/Shift 锁比例/框选新增/
 * zoom-to-cursor/双击 fit/Del 删除。变换实时进本地 preview,松手 dispatch updateCommand 闭环。
 * 几何字段名 + 命令 id 由 mod 经 view_state 声明(领域语义),通用交互全在此。
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useT, type ViewStateScope } from '@ze/core/04-user-facing/05-preferences'
import { SpatialCanvas, type SpatialBox } from './SpatialCanvas'
import { useViewport } from './useViewport'
import { useSpatialPointer } from './useSpatialPointer'
import { useEditorShortcuts, type ShortcutSpec } from '../lib/useEditorShortcuts'
import { applyMove } from './spatialMath'
import type { Box, Geometry, GeometryFieldMap, GizmoConstraints } from './spatialTypes'
import type { DispatchFn, EntityDict, StatusLookup } from '../types'

export type Canvas2dSpatialProps = {
  src?: string
  entities: EntityDict[]
  entityType: string
  selectedId: string | null
  /** F2 跨面板高亮通道(Rendering Hints):集合内实体画与选中态可区分、可叠加的描边。 */
  highlightedIds?: string[]
  onSelect: (id: string | null) => void
  geometry: GeometryFieldMap
  constraints: GizmoConstraints
  addCommand?: string
  updateCommand: string
  deleteCommand: string
  statusOf?: (id: string) => ReturnType<StatusLookup>
  /** 共享个人视图态作用域;记忆用户操作过的缩放/平移。 */
  viewStateScope?: ViewStateScope
  /** 视口持久化 key(每用户×每项目×每 slot):传了则记住缩放/平移、打开无缓存时自适应铺满。缺省=内存态(旧行为)。 */
  viewportPreferenceKey?: string
  /** opt-in:画完框后先弹一个文本输入(如「意图」),确认后 dispatch addCommand{type,box,[addPromptField]:文本}。
   *  不传 = 旧行为(画完框立即 dispatch{type,box})。让「框选+写意图」这类命令(如 add_by_box)有画布入口。 */
  addPromptLabel?: string
  addPromptField?: string
  addPromptPlaceholder?: string
  fire: DispatchFn
}

/** 从 entity.data 按映射读几何;读不全返 null(不渲染该 box)。 */
export function readGeometry(data: Record<string, unknown>, m: GeometryFieldMap): Geometry | null {
  const n = (v: unknown): number | null => {
    const x = typeof v === 'number' ? v : typeof v === 'string' ? Number(v) : NaN
    return Number.isFinite(x) ? x : null
  }
  if (m.kind === 'object') {
    const o = data[m.field]
    if (!o || typeof o !== 'object') return null
    const r = o as Record<string, unknown>
    const x = n(r.x), y = n(r.y), w = n(r.w), h = n(r.h)
    if (x === null || y === null || w === null || h === null) return null
    const rot = n(r.rotation)
    return { x, y, w, h, ...(rot !== null ? { rotation: rot } : {}) }
  }
  const x = n(data[m.x]), y = n(data[m.y]), w = n(data[m.w]), h = n(data[m.h])
  if (x === null || y === null || w === null || h === null) return null
  const rot = m.rotation ? n(data[m.rotation]) : null
  return { x, y, w, h, ...(rot !== null ? { rotation: rot } : {}) }
}

/** 几何 → entity.update 的 patches。 */
export function writeGeometryPatch(g: Geometry, m: GeometryFieldMap): Record<string, unknown> {
  const round = (v: number) => Math.round(v * 100) / 100
  if (m.kind === 'object') {
    return { [m.field]: { x: round(g.x), y: round(g.y), w: round(g.w), h: round(g.h), ...(m_hasRot(g) ? { rotation: round(g.rotation!) } : {}) } }
  }
  const p: Record<string, unknown> = { [m.x]: round(g.x), [m.y]: round(g.y), [m.w]: round(g.w), [m.h]: round(g.h) }
  if (m.rotation && g.rotation !== undefined) p[m.rotation] = round(g.rotation)
  return p
}
function m_hasRot(g: Geometry): boolean { return g.rotation !== undefined }

export function Canvas2dSpatial(props: Canvas2dSpatialProps): ReactNode {
  const { src, entities, entityType, selectedId, highlightedIds, onSelect, geometry, constraints, addCommand, updateCommand, deleteCommand, statusOf, viewStateScope, viewportPreferenceKey, addPromptLabel, addPromptField, addPromptPlaceholder, fire } = props
  const t = useT()
  const containerRef = useRef<HTMLDivElement>(null)
  const viewport = useViewport({ viewStateScope, storageKey: viewportPreferenceKey })
  const [loadedImage, setLoadedImage] = useState<{ src?: string; size: { w: number; h: number } }>()
  // src 切换后不把上一张图的自然尺寸短暂喂给新图的指针计算。
  const imageSize = loadedImage && loadedImage.src === src ? loadedImage.size : undefined
  const [mode, setMode] = useState<'select' | 'add'>('select')
  const initializedPreference = useRef<string | null>(null)
  // 每个作用域首次加载:无用户偏好则自适应;这次 fit 只是初始默认,绝不写入偏好。
  useEffect(() => {
    if (!imageSize || initializedPreference.current === viewport.preferenceIdentity) return
    const el = containerRef.current
    if (!el || el.clientWidth <= 0 || el.clientHeight <= 0) return
    initializedPreference.current = viewport.preferenceIdentity
    if (!viewport.hasStored) {
      viewport.fitTransient(imageSize, { w: el.clientWidth, h: el.clientHeight })
    }
  }, [imageSize, viewport.fitTransient, viewport.hasStored, viewport.preferenceIdentity])

  const handleImageLoad = (sz: { w: number; h: number }) => {
    setLoadedImage({ src, size: sz })
  }

  // 实体 → box(读几何;读不全跳过);highlighted = F2 跨面板高亮(与 selected 正交、可叠加)
  const boxes: SpatialBox[] = useMemo(() => {
    const hl = new Set(highlightedIds ?? [])
    const out: SpatialBox[] = []
    for (const e of entities) {
      if (e.deleted) continue
      const g = readGeometry(e.data, geometry)
      if (!g) continue
      out.push({
        id: e.id, geom: g, selected: e.id === selectedId, highlighted: hl.has(e.id),
        label: e.name, status: statusOf?.(e.id),
      })
    }
    return out
  }, [entities, geometry, selectedId, highlightedIds, statusOf])

  const commitGeometry = (id: string, g: Geometry) => {
    const e = entities.find((x) => x.id === id)
    if (!e) return
    fire(updateCommand, { type: entityType, id, patches: writeGeometryPatch(g, geometry), expected_version: e.version })
  }
  // 画完框:配了 addPromptLabel 则先弹文本输入收「意图」,确认后再 dispatch;否则立即 dispatch(旧行为)。
  const [pendingBox, setPendingBox] = useState<Box | null>(null)
  const [promptText, setPromptText] = useState('')
  const commitAdd = (box: Box) => {
    if (!addCommand) return
    if (addPromptLabel) { setPendingBox(box); setPromptText('') }
    else fire(addCommand, { type: entityType, box })
  }
  const confirmPrompt = () => {
    if (!addCommand || !pendingBox) return
    fire(addCommand, { type: entityType, box: pendingBox, [addPromptField || 'intent']: promptText.trim() })
    setPendingBox(null); setPromptText('')
  }
  const cancelPrompt = () => { setPendingBox(null); setPromptText('') }

  const sp = useSpatialPointer({
    containerRef, viewport, imageSize,
    boxes: boxes.map((b) => ({ id: b.id, geom: b.geom })),
    selectedId, constraints, mode, minAddSize: 6, addEnabled: !!addCommand,
    onSelect,
    onPreview: () => {},                 // 实时几何用 sp.preview(下方)直接渲染
    onCommitGeometry: commitGeometry,
    onCommitAdd: commitAdd,
  })

  // 拖拽中:选中 box 渲染到 preview;gizmo 跟随
  const selectedBox = boxes.find((b) => b.id === selectedId)
  const selectedGeom = sp.preview ?? selectedBox?.geom ?? null
  const boxesView = sp.preview && selectedId
    ? boxes.map((b) => (b.id === selectedId ? { ...b, geom: sp.preview! } : b))
    : boxes

  // 键盘:Esc 取消选,Del 删除,方向键 nudge(commit)
  const specs = useMemo<ShortcutSpec[]>(() => {
    const sel = () => entities.find((x) => x.id === selectedId)
    const nudge = (dx: number, dy: number) => {
      const e = sel(); if (!e) return
      const g = readGeometry(e.data, geometry); if (!g) return
      commitGeometry(e.id, applyMove(g, { x: dx, y: dy }))
    }
    return [
      { key: 'Escape', handler: () => onSelect(null) },
      { key: 'Delete', handler: () => { const e = sel(); if (e) fire(deleteCommand, { type: entityType, id: e.id, expected_version: e.version }) } },
      { key: 'Backspace', handler: () => { const e = sel(); if (e) fire(deleteCommand, { type: entityType, id: e.id, expected_version: e.version }) } },
      { key: 'ArrowLeft', shift: false, handler: () => nudge(-1, 0) },
      { key: 'ArrowRight', shift: false, handler: () => nudge(1, 0) },
      { key: 'ArrowUp', shift: false, handler: () => nudge(0, -1) },
      { key: 'ArrowDown', shift: false, handler: () => nudge(0, 1) },
    ]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, entities, geometry, entityType, deleteCommand])
  useEditorShortcuts(containerRef, specs)

  const hud = (
    <>
      <span>{Math.round(viewport.viewport.scale * 100)}%</span>
      <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
        onClick={() => { const el = containerRef.current; if (el && imageSize) viewport.fit(imageSize, { w: el.clientWidth, h: el.clientHeight }) }}>
        {t('editor.fit')}
      </button>
      {addCommand && (
        <button type="button" className="ze-button" data-variant={mode === 'add' ? 'primary' : 'ghost'} data-size="sm"
          onClick={() => setMode((m) => (m === 'add' ? 'select' : 'add'))}>
          {t('editor.addBox')}
        </button>
      )}
    </>
  )

  return (
    <>
      <SpatialCanvas
        src={src}
        viewport={viewport.viewport}
        boxes={boxesView}
        selectedGeom={selectedGeom}
        constraints={constraints}
        activeHandle={null}
        marquee={sp.marquee}
        containerRef={containerRef}
        bind={sp.bind}
        hud={hud}
        onImageLoad={handleImageLoad}
        dataKind={sp.kind === 'pan' ? 'pan' : sp.kind === 'move' ? 'move' : undefined}
      />
      {pendingBox && (
        <div
          className="ze-surface ze-round ze-pad ze-elevate ze-stack"
          data-surface="float" data-radius="md" data-p="3" data-gap="2"
          style={{ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%, -50%)', minWidth: '15rem' }}
        >
          <span className="ze-text" data-role="title">{addPromptLabel}</span>
          <input
            className="ze-input" data-size="sm" autoFocus value={promptText}
            placeholder={addPromptPlaceholder}
            onChange={(e) => setPromptText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); confirmPrompt() }
              else if (e.key === 'Escape') { e.preventDefault(); cancelPrompt() }
            }}
          />
          <div className="ze-row" data-gap="2" data-justify="end">
            <button type="button" className="ze-button" data-variant="ghost" data-size="sm" onClick={cancelPrompt}>{t('common.cancel')}</button>
            <button type="button" className="ze-button" data-variant="primary" data-size="sm" onClick={confirmPrompt}>{t('common.confirm')}</button>
          </div>
        </div>
      )}
    </>
  )
}
