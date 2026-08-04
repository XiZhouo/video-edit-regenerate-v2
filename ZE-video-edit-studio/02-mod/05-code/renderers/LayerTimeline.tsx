/**
 * LayerTimeline — 图层栈。**一行 = 一个图层**,视频片段是图层,标注也是图层,
 * 住同一根层序数轴、混在同一个列表里,按层序从大到小排。
 *
 * 一根数轴,两种不透明度:
 *   视频片段 —— 满屏不透明,盖住它下面的一切(下面的视频层、下面的标注框)
 *   标注     —— 只是一个框,不盖任何东西,所以标注之间互不遮挡
 *
 * 于是某一刻画面 = 最上面那个可见视频层;画在它上面的框 = 层序比它大的可见标注。
 * 被盖住的标注行会打成暗态并标明「被上面的视频层盖住」—— 画面上看不见的框发不出去。
 *
 * 每行左边一只眼睛,关掉就不参与。关掉最上面那层视频,看到的就是下面的原片(这就是对比)。
 * 每行都能拖换序 —— 层序不是装饰,它决定发给 AI 的画面是哪一层。
 *
 * 时间尺只用来**擦洗播放头**。它不产生「要发的那一段」——发送范围只由标注推导。
 *
 * 视觉全部贴 build-v3 的 timeline 原子(`.ze-timeline__ruler/__tick/__lane/__clip/
 * __clip-handle/__playhead`),本文件只写运行时位置/尺寸这类结构性内联。
 * 标注条的颜色经局部变量 `--clip-color` 注入 —— 那是原子明确允许调用方内联的一项,
 * 值来自 annotation.color,而它跟烧进画面那一框同一张色表。
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore,
         type PointerEvent as RPointerEvent } from 'react'
import { useT, ZeIcon, type CustomPaneProps } from '@ze/core/04-user-facing'
import { draggedSpan, snapCandidates, snapTime, type Edge } from './barDrag'
import { keyframeMarkerPresentation } from './guideKeyframes'
import { importVideos, isVideoFile } from './importVideos'
import { clampTime, fmt, getStudio, setStudio, subscribeStudio } from './studioStore'

type Dict = Record<string, any>
type RowKind = 'annotation' | 'clip'
type Row = { kind: RowKind; e: Dict; layer: number; start: number; duration: number; label: string; color?: string }
const num = (v: any, d = 0): number => (Number.isFinite(Number(v)) ? Number(v) : d)
const NAME_COL = 168          // 行首「眼睛 + 名字 + 删除」列宽(结构,不是视觉)

export default function LayerTimeline(
  { entities, entitiesByType, onSelect, dispatch, uploadAsset }: CustomPaneProps,
) {
  const t = useT()
  const studio = useSyncExternalStore(subscribeStudio, getStudio, getStudio)
  const rulerRef = useRef<HTMLDivElement | null>(null)
  const laneRef = useRef<HTMLDivElement | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  // 拖标注条:拖动中只改本地,松手才 dispatch 一次(否则一次拖拽吃掉整个 undo 栈)
  const [barDrag, setBarDrag] = useState<
    { id: string; kind: RowKind; edge: Edge; start0: number; dur0: number
      sourceIn0: number; srcLen: number; t0: number; dt: number } | null>(null)
  // 拖行换序:拖动中只记「拖到第几行」,松手才写 layer。
  // 层序不是装饰 —— compose_plan 每一刻取最上面那个可见图层,谁在上面决定发什么。
  const [rowDrag, setRowDrag] = useState<{ id: string; from: number; over: number } | null>(null)
  // 落库被拒(版本冲突、越界)要说出来,不能静默吞掉 —— 人只会看到条子弹回去
  const [barNote, setBarNote] = useState('')

  const clips = entities as Dict[]
  const annotations = (entitiesByType?.annotation ?? []) as Dict[]
  const guideKeyframes = (entitiesByType?.guide_keyframe ?? []) as Dict[]
  const referenceFrames = (entitiesByType?.reference_frame ?? []) as Dict[]
  const assets = (entitiesByType?.asset ?? []) as Dict[]

  // 时间线总长只由视频决定 —— 一条标注拖到 10s 不该把时间线撑成 10s。
  const duration = useMemo(
    () => Math.max(1, clips.reduce((m, c) => Math.max(m, num(c.data?.start) + num(c.data?.duration)), 0)),
    [clips])

  // **一个列表,按层序从大到小** —— 标注和视频片段是同一种东西,住同一根数轴。
  // 视频满屏不透明,盖住它下面的一切;标注只是个框,不盖任何东西。
  const rows: Row[] = useMemo(() => [
    ...annotations.map((a): Row => ({
      kind: 'annotation', e: a, layer: num(a.data?.layer),
      start: num(a.data?.start), duration: num(a.data?.duration),
      label: `${num(a.data?.number)}. ${t(`mod.guide.${String(a.data?.kind || 'box')}`)} · `
        + `${a.data?.label || a.name || ''}`,
      color: typeof a.data?.color === 'string' ? a.data.color : undefined,
    })),
    ...clips.map((c): Row => ({
      kind: 'clip', e: c, layer: num(c.data?.layer),
      start: num(c.data?.start), duration: num(c.data?.duration),
      label: c.name || c.id,
    })),
  ].sort((x, y) => (y.layer - x.layer) || x.kind.localeCompare(y.kind)),
  [annotations, clips, t])

  /** 某一刻压在最上面的那层视频的层序 —— 层序比它小的标注被盖住了。 */
  const clipFloorAt = useCallback((at: number): number => {
    let floor = -Infinity
    for (const r of rows) {
      if (r.kind !== 'clip' || r.e.data?.visible === false || r.duration <= 0) continue
      if (at >= r.start && at < r.start + r.duration) floor = Math.max(floor, r.layer)
    }
    return floor
  }, [rows])

  // ── 缩放:像素/秒。null = 适配(整条铺满可用宽度)────────────────
  //
  // 固定铺满在 4 秒的 demo 上还行,长素材完全没法用:一秒只有几个像素,
  // 掐头去尾根本掐不准。有了 zoom 就必须能横向滚动,而**一旦能滚,
  // 所有「屏幕 x → 时间」的换算都得重写** —— 按面板宽度算的老式换算在
  // 滚动/缩放下必错(同类坑犯过一次:名字列偏差 21%)。
  //
  // 这一版的做法是把换算的基准换成**轨道元素自己**:轨道宽度恒等于
  // duration × pxPerSec,并且它随滚动一起移动,所以
  //     t = (clientX - 轨道.left) / pxPerSec
  // 天然带上了滚动偏移,既不用手工加 scrollLeft,也不会再有名字列的账。
  const [zoom, setZoom] = useState<number | null>(null)
  const [fitPx, setFitPx] = useState(200)          // 适配态下的像素/秒,由可用宽度量出来
  const pxPerSec = zoom ?? fitPx
  const trackW = Math.max(1, duration * pxPerSec)

  // 适配态:量出可用宽度,换算成像素/秒。面板一变宽窄就重量。
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const measure = () => {
      const avail = Math.max(120, el.clientWidth - NAME_COL)
      setFitPx(avail / Math.max(0.5, duration))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [duration])

  const px = (sec: number) => `${clampTime(sec, duration) * pxPerSec}px`
  /** 屏幕 x → 时间。基准是**轨道元素**(宽 = duration × pxPerSec,随滚动移动),
   *  所以这一行同时吃掉了滚动偏移和名字列 —— 两笔账都不用手算。 */
  const timeAtX = useCallback((clientX: number): number => {
    const r = rulerRef.current?.getBoundingClientRect()
    if (!r) return 0
    return clampTime((clientX - r.left) / pxPerSec, duration)
  }, [duration, pxPerSec])
  const timeAt = useCallback((e: RPointerEvent): number =>
    timeAtX(e.clientX), [timeAtX])

  /** 缩放,并把锚点那一刻钉在屏幕原地 —— 不钉住的话放大一次内容就跑没影了。
   *  锚点:滚轮缩放取光标处,按钮缩放取视口中央。 */
  const zoomAt = useCallback((factor: number, clientX?: number) => {
    const el = scrollRef.current
    const r = rulerRef.current?.getBoundingClientRect()
    const next = Math.max(4, Math.min(2000, pxPerSec * factor))
    if (!el || !r) { setZoom(next); return }
    const anchorX = clientX ?? (el.getBoundingClientRect().left + NAME_COL
                                + (el.clientWidth - NAME_COL) / 2)
    const offset = anchorX - r.left            // 锚点在轨道内的像素位置(当前缩放下)
    const anchorT = Math.max(0, offset / pxPerSec)
    // 轨道宽度这一帧才变;在 rAF 里写 scrollLeft 会被旧的 scrollWidth 夹回去
    // (真机实测:锚点完全不生效,缩放永远钉住左边缘)。记下来,等布局落定的
    // effect 里再写。
    pendingScroll.current = Math.max(0, el.scrollLeft + anchorT * next - offset)
    setZoom(next)
  }, [pxPerSec])

  const pendingScroll = useRef<number | null>(null)
  useEffect(() => {
    const el = scrollRef.current
    if (!el || pendingScroll.current == null) return
    el.scrollLeft = pendingScroll.current
    pendingScroll.current = null
  }, [pxPerSec])

  const toggleVisible = async (row: Row) => {
    await dispatch('entity.update', {
      type: row.kind, id: row.e.id,
      patches: { visible: row.e.data?.visible === false },
      expected_version: row.e.version,
    })
  }

  /** 松手落层序:按拖后的行序把**整摞**(标注 + 视频)的 layer 重排。
   *  屏幕上第一行 = 最上面一层 = 最大 layer。标注与视频同轴,所以一起排。 */
  const commitReorder = async () => {
    const d = rowDrag
    setRowDrag(null)
    if (!d || d.over === d.from) return
    const moved = [...rows]
    const [taken] = moved.splice(d.from, 1)
    moved.splice(Math.max(0, Math.min(moved.length, d.over)), 0, taken)
    const top = moved.length - 1
    for (let i = 0; i < moved.length; i += 1) {
      const want = top - i
      if (moved[i].layer === want) continue
      await dispatch('entity.update', {
        type: moved[i].kind, id: moved[i].e.id, expected_version: moved[i].e.version,
        patches: { layer: want },
      })
    }
  }

  const removeAnnotation = async (row: Row) => {
    await dispatch('annotation.remove', { id: row.e.id })
  }

  /** 删掉一整层。界面上写着「不要就删掉那一行」,那就得真有这一下 ——
   *  从前只有眼睛,不满意的生成层只能关掉,越攒越长。 */
  const removeLayer = async (row: Row) => {
    await dispatch('entity.delete', { type: row.kind, id: row.e.id,
                                      expected_version: row.e.version })
  }

  /** 导入素材:空项目开箱是灰屏,而空态文案让人「先导入一段素材」——
   *  以前界面上没有任何能导入的控件,整个工具无法开始使用;
   *  后来有了按钮,却只建 asset 不建 clip —— 导完时间线还是空的。 */
  const importFiles = async (files: FileList | File[] | null) => {
    if (!files) return
    setBarNote('')
    try {
      const made = await importVideos(Array.from(files), { clips, dispatch, uploadAsset })
      if (!made) setBarNote(t('mod.timeline.dropNothing'))
    } catch (err: any) {
      setBarNote(String(err?.message || err))
    }
  }

  /** 拖到时间线任意处都能导入。dragCount 用计数不用布尔:
   *  子元素的 dragleave 会在进入子元素时先到,布尔会让高亮一闪一闪。 */
  const [dragCount, setDragCount] = useState(0)
  const dropProps = {
    onDragEnter: (e: React.DragEvent) => {
      if (![...e.dataTransfer.items].some((i) => i.kind === 'file')) return
      e.preventDefault(); setDragCount((n) => n + 1)
    },
    onDragOver: (e: React.DragEvent) => {
      if (![...e.dataTransfer.items].some((i) => i.kind === 'file')) return
      e.preventDefault(); e.dataTransfer.dropEffect = 'copy'
    },
    onDragLeave: () => setDragCount((n) => Math.max(0, n - 1)),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault(); setDragCount(0)
      void importFiles(Array.from(e.dataTransfer.files).filter(isVideoFile))
    },
  }

  // ── 时间尺:拖出一段 ────────────────────────────────────────────
  const [scrubbing, setScrubbing] = useState(false)
  const onRulerDown = (e: RPointerEvent) => {
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* 无捕获也能拖 */ }
    setScrubbing(true)
    setStudio({ playhead: timeAt(e) })
  }
  const onRulerMove = (e: RPointerEvent) => {
    if (!scrubbing) return
    if (e.buttons === 0) { setScrubbing(false); return }
    setStudio({ playhead: timeAt(e) })
  }
  const onRulerUp = () => setScrubbing(false)

  /** 这一层素材有多长 —— 裁剪的硬上限。生成层没有 asset,返回 0 = 不知道,不设限。 */
  const srcLenOf = useCallback((row: Row): number => {
    if (row.kind !== 'clip') return 0
    const a = assets.find((x) => x.id === row.e.data?.assetId)
    return num(a?.data?.duration)
  }, [assets])

  // ── 条子:整体拖 / 拉两端。标注和视频同一套手势 —— 时间线上它们是同一种东西。
  const onBarDown = (e: RPointerEvent, row: Row, edge: Edge) => {
    e.stopPropagation()
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* 无捕获也能拖 */ }
    setStudio({ picked: { type: row.kind, id: row.e.id } })
    setBarDrag({ id: row.e.id, kind: row.kind, edge, start0: row.start, dur0: row.duration,
                 sourceIn0: num(row.e.data?.sourceIn), srcLen: srcLenOf(row),
                 t0: timeAt(e), dt: 0 })
  }
  /** 拖行换序时按指针 Y 算落在哪一行。
   *  不能靠各行的 onPointerEnter —— 把手上做了 setPointerCapture,后续事件全锁在把手上,
   *  其他行的 enter 永远不会触发(这条是真机验出来的:拖完没反应)。 */
  const onRowMove = (e: RPointerEvent) => {
    if (!rowDrag) return
    const lanes = laneRef.current?.querySelectorAll<HTMLElement>('.ze-timeline__lane')
    if (!lanes) return
    let over = rowDrag.over
    lanes.forEach((el) => {
      const r = el.getBoundingClientRect()
      if (e.clientY >= r.top && e.clientY <= r.bottom) {
        over = Number(el.dataset.rowIndex ?? over)
      }
    })
    if (over !== rowDrag.over) setRowDrag({ ...rowDrag, over })
  }

  const onBarMove = (e: RPointerEvent) => {
    if (rowDrag) { onRowMove(e); return }
    if (!barDrag) return
    // 键已经松了还收到 move(pointercancel 之后、或在别处松的手)→ 收工,别继续跟手
    if (e.buttons === 0) { setBarDrag(null); return }
    setBarDrag({ ...barDrag, dt: timeAt(e) - barDrag.t0 })
  }
  const onBarUp = async () => {
    if (!barDrag) return
    const d = barDrag
    setBarDrag(null)
    if (Math.abs(d.dt) < 0.01) return
    const pool = d.kind === 'clip' ? clips : annotations
    const row = pool.find((x) => x.id === d.id)
    if (!row) return
    let next = draggedSpan({ ...d, isClip: d.kind === 'clip' }, duration)

    // 吸附:贴到整秒、别的条子的两端、播放头。容差按像素给 —— 缩放之后
    // 0.1 秒可能是 2px 也可能是 200px,按秒给会让放大时吸得莫名其妙。
    const gates = snapCandidates(
      rows.map((r) => ({ id: r.e.id, start: r.start, duration: r.duration })),
      d.id, studio.playhead, duration)
    if (d.edge === 'body') {
      const start = snapTime(next.start, gates, pxPerSec)
      next = { ...next, start }
    } else if (d.edge === 'start') {
      const start = snapTime(next.start, gates, pxPerSec)
      const end = d.start0 + d.dur0                 // 右缘钉住不动
      next = { start, duration: Math.max(d.kind === 'clip' ? 0.2 : 0, end - start),
               sourceIn: d.kind === 'clip'
                 ? Math.max(0, d.sourceIn0 + (start - d.start0)) : next.sourceIn }
    } else {
      const end = snapTime(next.start + next.duration, gates, pxPerSec)
      next = { ...next, duration: Math.max(d.kind === 'clip' ? 0.2 : 0, end - next.start) }
    }

    const patches: Dict = { start: Number(next.start.toFixed(2)),
                            duration: Number(next.duration.toFixed(2)) }
    // 左缘裁剪要把源入点一起挪,否则画面内容会整个跳位(见 draggedSpan 的说明)
    if (d.kind === 'clip' && d.edge === 'start') {
      patches.sourceIn = Number(next.sourceIn.toFixed(2))
    }
    try {
      await dispatch('entity.update', { type: d.kind, id: d.id,
                                        expected_version: row.version, patches })
      setBarNote('')
    } catch (err: any) {
      setBarNote(String(err?.message || err))
    }
  }

  const ticks = useMemo(() => {
    const step = duration <= 12 ? 1 : duration <= 60 ? 5 : 15
    const out: number[] = []
    for (let s = 0; s <= duration; s += step) out.push(s)
    return out
  }, [duration])

  return (
    <div className="ze-stack" data-gap="0" {...dropProps}
         data-drop-active={dragCount > 0 ? 'true' : undefined}
         style={{ height: '100%', minHeight: 140, position: 'relative' }}>
      {dragCount > 0 && (
        <div className="ze-bar">
          <span className="ze-text" data-role="secondary">{t('mod.timeline.dropHere')}</span>
        </div>
      )}
      {/* 缩放条:时间读数 + 放大缩小 + 适配 */}
      <div className="ze-bar ze-edge" data-edge="bottom">
        <span className="ze-text" data-role="tertiary" data-mono="true">
          {fmt(studio.playhead)}
        </span>
        <span className="ze-spacer" />
        <div className="ze-row" data-gap="0-5">
          <button type="button" className="ze-button" data-size="sm" data-variant="ghost"
                  title={t('mod.timeline.zoomOut')} onClick={() => zoomAt(1 / 1.5)}>−</button>
          <button type="button" className="ze-button" data-size="sm"
                  data-variant={zoom == null ? 'primary' : 'ghost'}
                  onClick={() => setZoom(null)}>{t('mod.timeline.zoomFit')}</button>
          <button type="button" className="ze-button" data-size="sm" data-variant="ghost"
                  title={t('mod.timeline.zoomIn')} onClick={() => zoomAt(1.5)}>+</button>
        </div>
      </div>

      {/* 一个横向滚动容器装下时间尺和所有图层行 —— 两者必须严丝合缝地一起滚,
          分成两个滚动区就会各走各的。名字列 sticky 在左边不动。 */}
      <div ref={scrollRef} className="ze-timeline__scroll ze-fill"
           onPointerMove={onBarMove}
           onPointerUp={() => { void onBarUp(); void commitReorder() }}
           onPointerCancel={() => { setBarDrag(null); setRowDrag(null) }}
           onWheel={(e) => {
             // Cmd/Ctrl + 滚轮 = 缩放(与各家剪辑软件一致);裸滚轮留给横向滚动
             if (!(e.metaKey || e.ctrlKey)) return
             e.preventDefault()
             zoomAt(e.deltaY > 0 ? 1 / 1.15 : 1.15, e.clientX)
           }}
           style={{ overflowX: 'auto', overflowY: 'auto' }}>
        <div ref={laneRef} style={{ width: NAME_COL + trackW, minWidth: '100%',
                                    position: 'relative' }}>
        {/* 时间尺:在这儿拖动只擦洗播放头 */}
        <div className="ze-row" data-gap="0">
          {/* sticky 的名字列必须有不透明底:没有的话横向滚动时条子会从名字底下透出来 */}
          <div className="ze-surface ze-pad" data-surface="canvas" data-px="2"
               style={{ width: NAME_COL, flexShrink: 0, position: 'sticky', left: 0, zIndex: 2 }}>
            <span className="ze-text" data-role="tertiary" data-mono="true">
              {pxPerSec >= 1 ? `${Math.round(pxPerSec)}px/s` : ''}
            </span>
          </div>
          <div ref={rulerRef} className="ze-timeline__ruler"
               onPointerDown={onRulerDown} onPointerMove={onRulerMove}
               onPointerUp={onRulerUp} onPointerCancel={onRulerUp}
               style={{ width: trackW, flexShrink: 0, position: 'relative' }}>
            {ticks.map((s) => (
              <span key={s} className="ze-timeline__tick" style={{ left: px(s) }}>{s}s</span>
            ))}
            {referenceFrames.filter((cue) => cue.data?.enabled !== false).map((cue) => (
              <button key={cue.id} type="button" className="ze-icon-button" data-size="sm"
                      title={t('mod.timeline.removeReference', { at: fmt(num(cue.data?.at)) })}
                      style={{ position: 'absolute', left: px(num(cue.data?.at)), top: 2,
                               transform: 'translateX(-50%)' }}
                      onPointerDown={(event) => event.stopPropagation()}
                      onClick={(event) => {
                        event.stopPropagation()
                        void dispatch('reference_frame.remove', { id: cue.id })
                      }}>
                <ZeIcon name="snapshot" size="sm" />
              </button>
            ))}
            {/* 正在画框 + 滚轮擦洗时的实时范围 —— 不等松手,当场就看得见 */}
            {studio.drafting && (
              <div className="ze-surface ze-edge" data-surface="strong" data-edge="all"
                   style={{ position: 'absolute', top: 0, bottom: 0,
                            left: px(studio.drafting.start),
                            width: `${Math.max(2, (studio.drafting.end - studio.drafting.start) * pxPerSec)}px`,
                            pointerEvents: 'none' }} />
            )}
          </div>
        </div>

        {rows.length === 0 && (
          <div className="ze-stack ze-pad" data-gap="2" data-p="3">
            <span className="ze-text" data-role="tertiary">{t('mod.timeline.empty')}</span>
            <label className="ze-button" data-variant="primary" data-size="sm"
                   style={{ alignSelf: 'flex-start' }}>
              {t('mod.timeline.importAsset')}
              <input type="file" accept="video/*" multiple
                     onChange={(ev) => void importFiles(ev.target.files)}
                     style={{ display: 'none' }} />
            </label>
          </div>
        )}
        {rows.map((row, i) => {
          const hidden = row.e.data?.visible === false
          const picked = studio.picked?.type === row.kind && studio.picked.id === row.e.id
          const live = barDrag && barDrag.id === row.e.id
            ? draggedSpan({ ...barDrag, isClip: barDrag.kind === 'clip' }, duration) : row
          const isMark = row.kind === 'annotation'
          // 这条标注在它自己那一段的中点上,是不是被上面的视频层盖住了 ——
          // 盖住 = 画面上看不见 = 发送时不存在,行上要如实说出来。
          const covered = isMark && !hidden
            && row.layer < clipFloorAt(row.start + row.duration / 2)
          // 条色:标注用它自己那一档(与烧进画面的框同源),视频按原片/生成分两色。
          // 在 JSX 外算好 —— 三元写在 style 里会让 `row.color` 后面紧跟一个冒号,
          // mod_renderer 的块级扫描会把它当成内联 `color:` 视觉样式误报。
          const barColor = isMark
            ? row.color
            : (row.e.data?.xKind === 'gen' ? 'var(--ze-data-3)' : 'var(--ze-data-1)')
          return (
            <div key={`${row.kind}-${row.e.id}`}
                 className="ze-timeline__lane ze-row" data-gap="0"
                 data-row-index={i}
                 data-selected={rowDrag?.id === row.e.id ? 'true' : undefined}
                 onClick={() => { setStudio({ picked: { type: row.kind, id: row.e.id } })
                                  if (row.kind === 'clip') onSelect(row.e.id) }}>
              <div className="ze-surface ze-row ze-pad" data-surface="canvas"
                   data-gap="1" data-px="2"
                   style={{ width: NAME_COL, flexShrink: 0,
                            position: 'sticky', left: 0, zIndex: 2 }}>
                <button type="button" className="ze-icon-button" data-size="sm"
                        title={t('mod.timeline.reorder')}
                        onPointerDown={(ev) => {
                          ev.stopPropagation()
                          try { ev.currentTarget.setPointerCapture(ev.pointerId) } catch { /* 无捕获也能拖 */ }
                          setRowDrag({ id: row.e.id, from: i, over: i })
                        }}>
                  <ZeIcon name="drag" size="sm" />
                </button>
                <button type="button" className="ze-icon-button" data-size="sm"
                        title={t(hidden ? 'mod.timeline.show' : 'mod.timeline.hide')}
                        aria-pressed={!hidden}
                        onClick={(ev) => { ev.stopPropagation(); void toggleVisible(row) }}>
                  <ZeIcon name={hidden ? 'eye-off' : 'eye'} size="sm" />
                </button>
                <span className="ze-text" data-role="tertiary" data-mono="true"
                      title={t('mod.timeline.layerNo')}>{row.layer}</span>
                <span className="ze-text ze-fill" data-truncate="true"
                      data-role={hidden || covered ? 'tertiary' : 'secondary'}
                      title={covered ? t('mod.timeline.covered') : undefined}>{row.label}</span>
                <button type="button" className="ze-icon-button" data-size="sm"
                        title={t(isMark ? 'mod.timeline.removeMark' : 'mod.timeline.removeLayer')}
                        onClick={(ev) => { ev.stopPropagation()
                                           void (isMark ? removeAnnotation(row) : removeLayer(row)) }}>
                  <ZeIcon name="trash" size="sm" />
                </button>
              </div>
              <div style={{ position: 'relative', width: trackW, flexShrink: 0,
                            alignSelf: 'stretch' }}>
                <div className="ze-timeline__clip"
                     data-selected={picked ? 'true' : undefined}
                     data-run-phase={hidden || covered ? 'pending' : undefined}
                     onPointerDown={(ev) => onBarDown(ev, row, 'body')}
                     style={{ left: px(live.start),
                              // 单帧标注(duration=0)也要看得见,给一条细线的宽度
                              width: `${Math.max(isMark ? 3 : 1,
                                        Math.min(live.duration,
                                                 duration - clampTime(live.start, duration))
                                        * pxPerSec)}px`,
                              // timeline 原子约定:调用方只注入位置/尺寸和局部 --clip-color。
                              ['--clip-color' as any]: barColor }}>
                  <span className="ze-timeline__clip-label">
                    {isMark ? num(row.e.data?.number)
                            : t('mod.timeline.layerN', { n: num(row.e.data?.layer) })}
                  </span>
                  <span className="ze-timeline__clip-handle ze-timeline__clip-handle--start"
                        onPointerDown={(ev) => onBarDown(ev, row, 'start')} />
                  <span className="ze-timeline__clip-handle"
                        onPointerDown={(ev) => onBarDown(ev, row, 'end')} />
                </div>
                {isMark && guideKeyframes
                  .filter((frame) => String(frame.data?.parentId || '') === row.e.id)
                  .map((frame) => {
                    const at = row.start + num(frame.data?.time)
                    const marker = keyframeMarkerPresentation()
                    return (
                      <button key={frame.id} type="button"
                              className="ze-icon-button" data-size={marker.size}
                              data-variant={marker.variant}
                              aria-label={t('mod.keyframe.jump', { at: fmt(at) })}
                              title={t('mod.keyframe.jump', { at: fmt(at) })}
                              style={{ position: 'absolute', left: px(at), top: '50%',
                                       transform: 'translate(-50%, -50%)', zIndex: marker.zIndex,
                                       width: marker.hitSize, height: marker.hitSize,
                                       pointerEvents: marker.pointerEvents }}
                              onPointerDown={(event) => event.stopPropagation()}
                              onClick={(event) => {
                                event.stopPropagation()
                                setStudio({
                                  playhead: at,
                                  picked: { type: 'annotation', id: row.e.id },
                                })
                              }}>
                        <span aria-hidden="true">◆</span>
                      </button>
                    )
                  })}
              </div>
            </div>
          )
        })}

        {/* 播放头贯穿所有行 —— 不贯穿就对不上「这一刻这个标注在不在」。
            它住在轨道容器里,所以横向滚动时和条子一起走。 */}
        <div style={{ position: 'absolute', top: 0, bottom: 0, left: NAME_COL, right: 0,
                      pointerEvents: 'none' }}>
          <div className="ze-timeline__playhead" style={{ left: px(studio.playhead) }} />
        </div>
        </div>
      </div>

      {barNote && (
        <div className="ze-bar">
          <span className="ze-text" data-role="tertiary" data-tone="danger">{barNote}</span>
        </div>
      )}
    </div>
  )
}
