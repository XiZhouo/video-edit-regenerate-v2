/**
 * LayerCanvas — 主工作区:播放**合成结果**,并在画面上直接圈出要改的地方。
 *
 * 时间线是一摞图层。任意时刻画面上看到的,是那一刻**最上面那个可见图层**;
 * 标注也是图层,画在最上面。关掉谁的眼睛,谁就从画面上消失。
 *
 * ## 工具(对齐草图设计 v1 的 activeTool 机制)
 *
 *   select(默认,V / 1)—— 点框选中、拖框移动、拖角改大小;**点空白什么都不建**
 *   rect  (R / 2)      —— 拉框建标注,落定后自动切回 select
 *   arrow (A / 4)      —— 按下放箭尾,按住拖动预览,松开放箭头;保持工具以连续添加
 *
 * 默认是 select 这件事本身就是需求:没挑「画框」之前,画布上按一下不该凭空长出东西。
 *
 * ## rect 的一次手势同时定下空间和时间(规划「区间手势状态机」定案)
 *
 *   按下   → 落框起点,锚帧 ta = 当前播放头
 *   拖动   → 改框(空间)
 *   未松手滚轮 → 擦洗播放头,区间 = 擦洗触及的 [min, max];一格 3 帧,按住 Shift 1 帧
 *   松手   → 标注落定;没滚过轮就退化成单帧(duration=0,合法)
 *
 * 为什么是一次手势:"这东西从什么时候错到什么时候",人判断它的唯一办法是往前后拨着看。
 * 拆成"先画框、再填两个时间数字"就是把一次判断劈成两次机械劳动。
 *
 * ## 边界
 * 本组件不持有业务逻辑 —— 手势只翻译成命令参数,落 `annotation.add` / `entity.update`。
 * AI 用同一条命令能建出一模一样的标注(标准 P6:CLI/AI 面与 Web 面同源)。
 * 拖动中只改本地 draft,**松手才 dispatch 一次** —— 否则一次拖拽吃掉整个 undo 栈。
 *
 * 视觉全部贴 build-v3 原子:`.ze-spatial` + `__overlay/__box/__marquee/__hud`、
 * `.ze-gizmo__handle`。本文件只写结构性内联(位置/尺寸)。
 * 命中检测走 JS 数学而不是 DOM:`.ze-spatial__overlay` 是 `pointer-events:none`
 * (L2 原子定的,mod 不许覆盖),SVG 里的 rect 永远点不中。
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore,
         type PointerEvent as RPointerEvent, type WheelEvent as RWheelEvent } from 'react'
import { useT, type CustomPaneProps } from '@ze/core/04-user-facing'
import { canvasRectOf, isIdentity, rectToPercent } from './clipGeometry'
import { boxOf, clamp100, movedBox, type Box, type Grip, type Pt } from './canvasGeometry'
import { importGuideImages, importVideos, isImageFile, isVideoFile } from './importVideos'
import { chooseStableMediaSource, type MediaSource } from './mediaSource'
import { deletionForKey } from './selectionDeletion'
import {
  DEFAULT_GUIDE_COLOR, GUIDE_COLORS, arrowMove, arrowPress, arrowRelease, guideColorControlMode,
  isGuideColorEditable,
  type ArrowDraft, type GuideColor,
} from './guideTools'
import {
  keyframeAt, keyframesFor, resolveGuideGeometry, type GuideKeyframe,
} from './guideKeyframes'
import {
  clampTime, fmt, getStudio, setStudio, subscribeStudio, toolActivationPatch, type ToolId,
} from './studioStore'

type Dict = Record<string, any>
type DrawGuideKind = 'box' | 'text'
/** 四种适配方式,与域层 FIT_MODES 同序 */
const FITS = ['contain', 'cover', 'stretch', 'none'] as const

const FPS = 30
const WHEEL_FRAMES = 3          // 滚轮一格 3 帧;按住 Shift 一帧(规划定案)
const MIN_BOX = 0.6             // 框小于画面的 0.6% 当误触,不建标注
const HANDLE = 2.2              // 角手柄命中半径(画面百分比)

const num = (v: any, d = 0): number => (Number.isFinite(Number(v)) ? Number(v) : d)
function arrowOf(guide: Dict, canvasW: number, canvasH: number): Pt[] {
  try {
    const raw = JSON.parse(String(guide.data?.pointsJson || '[]'))
    if (Array.isArray(raw) && raw.length >= 2) {
      return raw.map((point) => ({
        x: (num(point?.x) / canvasW) * 100,
        y: (num(point?.y) / canvasH) * 100,
      }))
    }
  } catch { /* 旧数据回退 bbox */ }
  const d = guide.data || {}
  return [
    { x: (num(d.bboxX) / canvasW) * 100, y: (num(d.bboxY) / canvasH) * 100 },
    { x: ((num(d.bboxX) + num(d.bboxW)) / canvasW) * 100,
      y: ((num(d.bboxY) + num(d.bboxH)) / canvasH) * 100 },
  ]
}

function arrowHead(points: Pt[]): [Pt, Pt][] {
  if (points.length < 2) return []
  const a = points[points.length - 2], b = points[points.length - 1]
  const angle = Math.atan2(b.y - a.y, b.x - a.x)
  const size = 3
  return [
    [b, { x: b.x - size * Math.cos(angle - Math.PI / 6),
          y: b.y - size * Math.sin(angle - Math.PI / 6) }],
    [b, { x: b.x - size * Math.cos(angle + Math.PI / 6),
          y: b.y - size * Math.sin(angle + Math.PI / 6) }],
  ]
}

/** 可见图层,从下往上。隐藏的行不参与合成 —— 这是人控制"这次发什么"的唯一开关。 */
function visibleLayers(clips: Dict[]): Dict[] {
  return clips
    .filter((c) => c.data?.visible !== false && num(c.data?.duration) > 0)
    .sort((a, b) => num(a.data?.layer) - num(b.data?.layer))
}

/** t 时刻画面上看到的是哪一层 —— 最上面那个盖住这一刻的可见图层。 */
function topmostAt(clips: Dict[], t: number): Dict | null {
  const hit = visibleLayers(clips).filter((c) => {
    const s = num(c.data?.start)
    return t >= s && t < s + num(c.data?.duration)
  })
  return hit.length ? hit[hit.length - 1] : null
}

/** 这一刻画面上该出现哪些标注。
 *
 *  三个条件:没关眼睛、播放头落在它那一段里、**层序在当前那层视频之上**。
 *  最后一条就是「看见什么就发什么」—— 视频片段满屏不透明,盖住它下面的一切
 *  (下面的视频层、下面的标注框);标注只是个框,不盖任何东西。
 *
 *  容差取半帧,不是 1e-3:start 落库时会被 toFixed(2) 舍到百分秒,
 *  播放头 3.457 存成 3.46,差 0.003 —— 比 1e-3 大,单帧标注就永远画不出来。 */
function annotationsAt(annotations: Dict[], t: number, topClip: Dict | null): Dict[] {
  const eps = 0.5 / FPS
  const floor = topClip ? num(topClip.data?.layer) : -Infinity
  return annotations.filter((a) => {
    if (a.data?.visible === false) return false
    if (num(a.data?.layer) < floor) return false      // 被上面那层视频盖住了
    const s = num(a.data?.start)
    return t >= s - eps && t <= s + num(a.data?.duration) + eps
  })
}

export default function LayerCanvas(
  { entities, entitiesByType, dispatch, assetUrl, uploadAsset }: CustomPaneProps,
) {
  const t = useT()
  const studio = useSyncExternalStore(subscribeStudio, getStudio, getStudio)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const surfaceRef = useRef<HTMLDivElement | null>(null)
  const creationInputRef = useRef<HTMLInputElement | null>(null)

  // 拖动中的临时态。存「按下点 + 当前点」而不是「一个框」:框每帧从两点重算,
  // 往左上拖也对(存框的话 origin 会被上一帧的 min 冲掉,反向拖会越拖越小)。
  const [draft, setDraft] = useState<
    { grip: Grip; guideKind?: DrawGuideKind; annId?: string; base?: Box;
      from: Pt; to: Pt; lo: number; hi: number } | null>(null)
  const [arrowDraft, setArrowDraft] = useState<ArrowDraft | null>(null)
  const [guideColor, setGuideColor] = useState<GuideColor>(DEFAULT_GUIDE_COLOR)
  const [label, setLabel] = useState('')
  const [note, setNote] = useState('')

  const clips = entities as Dict[]
  const assets = (entitiesByType?.asset ?? []) as Dict[]
  const annotations = (entitiesByType?.annotation ?? []) as Dict[]
  const guideKeyframes = (entitiesByType?.guide_keyframe ?? []) as GuideKeyframe[]
  const projects = (entitiesByType?.project ?? []) as Dict[]

  // 标注 bbox 用的是项目画布坐标系;画面按这个比例居中摆,框才落在画面上而不是黑边上。
  const canvasW = num(projects[0]?.data?.width, 1920) || 1920
  const canvasH = num(projects[0]?.data?.height, 1080) || 1080
  const projectFps = Math.max(1, num(projects[0]?.data?.fps, FPS))

  const duration = useMemo(
    () => clips.reduce((m, c) => Math.max(m, num(c.data?.start) + num(c.data?.duration)), 0),
    [clips])
  useEffect(() => { setStudio({ duration }) }, [duration])

  const top = useMemo(() => topmostAt(clips, studio.playhead), [clips, studio.playhead])

  /** 这一层的视频:生成层自己带 videoRef;原片层回到它引用的素材。 */
  const sourceCandidate = useMemo((): MediaSource => {
    if (!top || !assetUrl) return { key: '', url: '' }
    const own = top.data?.videoRef
    if (own?.sha256) return { key: own.sha256, url: assetUrl(own) }
    const asset = assets.find((a) => a.id === top.data?.assetId)
    const ref = asset?.data?.assetRef
    return ref?.sha256 ? { key: ref.sha256, url: assetUrl(ref) } : { key: '', url: '' }
  }, [top, assets, assetUrl])
  const [stableSource, setStableSource] = useState<MediaSource>({ key: '', url: '' })
  useEffect(() => {
    setStableSource((current) => chooseStableMediaSource(current, sourceCandidate))
  }, [sourceCandidate.key, sourceCandidate.url])
  // Switch immediately for different media, but keep the loaded URL when only its ticket rotates.
  const src = stableSource.key === sourceCandidate.key ? stableSource.url : sourceCandidate.url

  /** 播放头 → 该层内部的时间。**播放中不反向 seek** —— 播放时是视频驱动播放头,
   *  两个方向同时互写会自激抖动。 */
  useEffect(() => {
    const el = videoRef.current
    if (!el || !top || studio.playing) return
    const local = studio.playhead - num(top.data?.start) + num(top.data?.sourceIn)
    if (Math.abs(el.currentTime - local) > 0.05) el.currentTime = Math.max(0, local)
  }, [studio.playhead, studio.playing, top])

  /** 播放中把 video.currentTime 写回播放头 —— 没有这一段,「能播放」就是假的:
   *  标注不出现不消失、时间线播放头不动、上层图层也不随时间切换。
   *
   *  两个坑,都被真机验出来过:
   *  ① 这个 effect 依赖 top/src,播放中换层(或任何让 top 拿到新引用的更新)会重跑,
   *     rAF 被 cancel 而 play 事件不会再来一次 → 循环停了、画面还在播,播放头冻住。
   *     所以启动条件不是「收到 play 事件」,而是「此刻 el 没暂停」。
   *  ② <video> 卸载(这一刻没有可见图层)时 pause/ended 都送不到,playing 会永久
   *     latch 成 true,之后反向 seek 与播放按钮双双失灵。所以卸载时在清理里落回 false。 */
  useEffect(() => {
    const el = videoRef.current
    if (!el) { setStudio({ playing: false }); return }
    let raf = 0
    const tick = () => {
      const cur = videoRef.current
      if (!cur || cur.paused) { setStudio({ playing: false }); return }
      const at = num(top?.data?.start) + (cur.currentTime - num(top?.data?.sourceIn))
      if (Math.abs(at - getStudio().playhead) > 1 / FPS) {   // 节流:超过一帧才写
        setStudio({ playhead: clampTime(at, getStudio().duration) })
      }
      raf = requestAnimationFrame(tick)
    }
    const start = () => { setStudio({ playing: true }); cancelAnimationFrame(raf); raf = requestAnimationFrame(tick) }
    const stop = () => { setStudio({ playing: false }); cancelAnimationFrame(raf) }
    const done = () => { wantPlay.current = false; stop() }
    el.addEventListener('play', start)
    el.addEventListener('pause', stop)
    el.addEventListener('ended', done)
    if (!el.paused) start()          // effect 重跑时自愈,不等下一次 play 事件
    else if (wantPlay.current) void el.play()   // 换层后接着播,别停死在接缝上
    return () => {
      cancelAnimationFrame(raf)
      el.removeEventListener('play', start)
      el.removeEventListener('pause', stop)
      el.removeEventListener('ended', done)
      setStudio({ playing: false })
    }
  }, [top, src])

  /** 指针位置 → 画面百分比坐标(量在舞台上,舞台就是画面本身)。 */
  const ptOf = useCallback((e: RPointerEvent): Pt => {
    const r = surfaceRef.current?.getBoundingClientRect()
    if (!r) return { x: 0, y: 0 }
    return { x: ((e.clientX - r.left) / r.width) * 100,
             y: ((e.clientY - r.top) / r.height) * 100 }
  }, [])

  /** 选中的那条标注(时间线和画布共用一个选中态) */
  const pickedAnn = useMemo(
    () => (studio.picked?.type === 'annotation'
      ? annotations.find((a) => a.id === studio.picked!.id) ?? null : null),
    [annotations, studio.picked])
  const pickedFrames = useMemo(
    () => pickedAnn ? keyframesFor(guideKeyframes, pickedAnn.id) : [],
    [guideKeyframes, pickedAnn],
  )
  const pickedLocalTime = pickedAnn
    ? Math.max(0, studio.playhead - num(pickedAnn.data?.start)) : 0
  const currentKeyframe = keyframeAt(pickedFrames, pickedLocalTime, projectFps)
  const canAnimatePicked = pickedAnn
    ? ['box', 'image'].includes(String(pickedAnn.data?.kind || 'box')) : false
  const pickedInRange = pickedAnn
    ? studio.playhead >= num(pickedAnn.data?.start) - 0.5 / projectFps
      && studio.playhead <= num(pickedAnn.data?.start) + num(pickedAnn.data?.duration)
        + 0.5 / projectFps
    : false
  const [labelDraft, setLabelDraft] = useState('')
  // 「人要不要在播」这个意图必须活过 <video> 换元素:播到图层接缝时 src 一换,
  // 新元素默认是暂停的 —— 不记住意图,播放就停死在接缝上,还得人再按一次。
  const wantPlay = useRef(false)
  const draftFor = useRef<string | null>(null)
  useEffect(() => {
    // 换了选中对象才重置草稿;同一条实体因为别处更新拿到新引用时不覆盖正在打的字
    if (draftFor.current !== (pickedAnn?.id ?? null)) {
      draftFor.current = pickedAnn?.id ?? null
      setLabelDraft(String(pickedAnn?.data?.label ?? ''))
    }
  }, [pickedAnn])
  useEffect(() => {
    if (studio.tool === 'text' && !pickedAnn) creationInputRef.current?.focus()
  }, [studio.tool, pickedAnn])

  useEffect(() => {
    const onDeleteKey = (event: KeyboardEvent) => {
      const deletion = deletionForKey(event, studio.picked, annotations, clips)
      if (!deletion) return
      event.preventDefault()
      void Promise.resolve(dispatch(deletion.commandId, deletion.payload))
        .then(() => {
          setStudio({ picked: null })
          setNote('')
        })
        .catch((err: any) => setNote(String(err?.message || err)))
    }
    window.addEventListener('keydown', onDeleteKey)
    return () => window.removeEventListener('keydown', onDeleteKey)
  }, [annotations, clips, dispatch, studio.picked])

  const commitLabel = async () => {
    if (!pickedAnn) return
    const next = labelDraft.trim()
    if (!next || next === String(pickedAnn.data?.label ?? '')) return
    try {
      await dispatch('entity.update', {
        type: 'annotation', id: pickedAnn.id, expected_version: pickedAnn.version,
        patches: { label: next },
      })
      setNote('')
    } catch (err: any) {
      setNote(String(err?.message || err))
    }
  }

  const commitPickedColor = async (color: GuideColor) => {
    if (!pickedAnn || !isGuideColorEditable(String(pickedAnn.data?.kind || 'box'))) return
    if (String(pickedAnn.data?.color || DEFAULT_GUIDE_COLOR).toLowerCase() === color) return
    try {
      await dispatch('entity.update', {
        type: 'annotation', id: pickedAnn.id, expected_version: pickedAnn.version,
        patches: { color },
      })
      setNote('')
    } catch (err: any) {
      setNote(String(err?.message || err))
    }
  }

  /** 这一刻该画哪些标注 */
  const shown = useMemo(
    () => annotationsAt(annotations, studio.playhead, top), [annotations, studio.playhead, top])

  const guideImageUrl = useCallback((guide: Dict): string => {
    if (!assetUrl || String(guide.data?.kind || '') !== 'image') return ''
    const asset = assets.find((a) => a.id === guide.data?.refImageAssetId)
    const ref = asset?.data?.processedRef || asset?.data?.assetRef
    return ref?.sha256 ? assetUrl(ref) : ''
  }, [assets, assetUrl])

  // ── 选中的视频图层:摆它进画布 ────────────────────────────────────
  //
  // 只在「这一层此刻就在画面上」时给手柄。摆一层看不见的素材是盲摆 ——
  // 拖完不知道摆成了什么样,等于让人对着黑屏调参数。

  const pickedClip = useMemo(
    () => (studio.picked?.type === 'clip'
      ? clips.find((c) => c.id === studio.picked!.id) ?? null : null),
    [clips, studio.picked])
  const clipOnScreen = !!pickedClip && top?.id === pickedClip.id

  /** 这一层素材的原始尺寸。优先取素材实测(导入时 ffprobe 落的),
   *  取不到就回落到正在播的那个 <video> 的真实尺寸 —— 生成层没有 asset。 */
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  useEffect(() => { setNatural(null) }, [src])
  const srcSize = useMemo(() => {
    const a = assets.find((x) => x.id === pickedClip?.data?.assetId)
    const aw = num(a?.data?.width), ah = num(a?.data?.height)
    if (aw > 0 && ah > 0) return { w: aw, h: ah }
    if (clipOnScreen && natural) return natural
    return { w: canvasW, h: canvasH }        // 什么都问不到:当它正好等于画布
  }, [assets, pickedClip, clipOnScreen, natural, canvasW, canvasH])

  /** 这一层的画面落在画布哪块(舞台百分比)—— 与渲染同一套算式(clipGeometry 钉子表)。 */
  const clipRect = useMemo((): Box | null => {
    if (!pickedClip) return null
    const r = rectToPercent(
      canvasRectOf(pickedClip.data as any,
                   { width: canvasW, height: canvasH, srcW: srcSize.w, srcH: srcSize.h }),
      canvasW, canvasH)
    return { x: r.x, y: r.y, w: r.width, h: r.height }
  }, [pickedClip, canvasW, canvasH, srcSize])

  /** 拖动中的实时框:拖的时候手柄要跟着手走,不能等松手才动。 */
  const clipLive = useMemo((): Box | null => {
    if (!clipRect) return null
    if (!draft || (draft.grip !== 'clipMove' && draft.grip !== 'clipScale')) return clipRect
    if (draft.grip === 'clipMove') {
      return { ...clipRect,
               x: clipRect.x + (draft.to.x - draft.from.x),
               y: clipRect.y + (draft.to.y - draft.from.y) }
    }
    const k = scaleFactor(clipRect, draft)
    const cx = clipRect.x + clipRect.w / 2, cy = clipRect.y + clipRect.h / 2
    const w = clipRect.w * k, h = clipRect.h * k
    return { x: cx - w / 2, y: cy - h / 2, w, h }
  }, [clipRect, draft])

  /** 预览要照着这一层的几何摆 —— 否则「看见什么就发送什么」当场破了:
   *  人在画面上圈的位置,和合成时素材真正所在的位置对不上。
   *
   *  fitMode → object-fit(none = 原始像素,与域层同义);位移/缩放 → transform。
   *  translate 的百分比是相对元素自己的盒子,而这个盒子就是画布,所以 `x` 这个
   *  「画布宽的比例」直接乘 100 就是它。
   *
   *  裁切(cropL/T/R/B)预览暂不体现:CSS 是先适配再裁,域层是先裁再适配,
   *  拿 clip-path 硬凑会和渲染对不上 —— 宁可不画。裁切走属性面板的数值,
   *  拖拽裁切手柄留作后续。 */
  const topGeomStyle = useMemo((): Dict => {
    const d = top?.data ?? {}
    const fit = String(d.fitMode || 'contain')
    const objectFit = fit === 'stretch' ? 'fill' : fit === 'none' ? 'none' : fit
    const s = d.scale == null ? 1 : num(d.scale, 1)
    const tx = num(d.x) * 100, ty = num(d.y) * 100
    const live = draft && top?.id === pickedClip?.id
      ? (draft.grip === 'clipMove'
          ? { tx: tx + (draft.to.x - draft.from.x), ty: ty + (draft.to.y - draft.from.y), s }
          : draft.grip === 'clipScale' && clipRect
            ? { tx, ty, s: s * scaleFactor(clipRect, draft) }
            : { tx, ty, s })
      : { tx, ty, s }
    return { objectFit,
             transform: `translate(${live.tx}%, ${live.ty}%) scale(${live.s})` }
  }, [top, draft, pickedClip, clipRect])

  const applyClip = async (patches: Dict) => {
    if (!pickedClip) return
    try {
      await dispatch('entity.update', {
        type: 'clip', id: pickedClip.id, expected_version: pickedClip.version, patches,
      })
      setNote('')
    } catch (err: any) {
      setNote(String(err?.message || err))
    }
  }

  /** 标注 → 当前播放头的画面百分比框。没有关键画面时保持旧项目的静态位置。 */
  const rectOf = useCallback((a: Dict): Box => {
    const base = {
      x: num(a.data?.bboxX), y: num(a.data?.bboxY),
      w: num(a.data?.bboxW), h: num(a.data?.bboxH),
    }
    const frames = keyframesFor(guideKeyframes, a.id)
    const geometry = resolveGuideGeometry(
      base,
      frames,
      Math.max(0, studio.playhead - num(a.data?.start)),
    )
    return {
      x: (geometry.x / canvasW) * 100, y: (geometry.y / canvasH) * 100,
      w: (geometry.w / canvasW) * 100, h: (geometry.h / canvasH) * 100,
    }
  }, [canvasW, canvasH, guideKeyframes, studio.playhead])

  const setPickedKeyframe = async (box = pickedAnn ? rectOf(pickedAnn) : null) => {
    if (!pickedAnn || !box || !canAnimatePicked || !pickedInRange) return
    try {
      await dispatch('guide.keyframe.set', {
        annotationId: pickedAnn.id,
        at: studio.playhead,
        bboxX: Math.round((box.x / 100) * canvasW),
        bboxY: Math.round((box.y / 100) * canvasH),
        bboxW: Math.round((box.w / 100) * canvasW),
        bboxH: Math.round((box.h / 100) * canvasH),
      })
      setNote('')
    } catch (err: any) {
      setNote(String(err?.message || err))
    }
  }

  const removeCurrentKeyframe = async () => {
    if (!currentKeyframe?.id) return
    try {
      await dispatch('guide.keyframe.remove', { id: currentKeyframe.id })
      setNote('')
    } catch (err: any) {
      setNote(String(err?.message || err))
    }
  }

  /** 命中检测:从上往下(编号大的在上)找第一个盖住这一点的标注,并判是不是压在角上。 */
  const hitTest = useCallback((p: Pt): { ann: Dict; grip: Grip } | null => {
    const ordered = [...shown].sort((a, b) => num(b.data?.number) - num(a.data?.number))
    for (const a of ordered) {
      const r = rectOf(a)
      const corners: [Grip, number, number][] = [
        ['nw', r.x, r.y], ['ne', r.x + r.w, r.y],
        ['sw', r.x, r.y + r.h], ['se', r.x + r.w, r.y + r.h],
      ]
      for (const [grip, cx, cy] of corners) {
        if (Math.abs(p.x - cx) <= HANDLE && Math.abs(p.y - cy) <= HANDLE) return { ann: a, grip }
      }
      if (p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h) {
        return { ann: a, grip: 'move' }
      }
    }
    return null
  }, [shown, rectOf])

  /** 这一点是不是压在「选中图层」的变换框上 —— 压在角上是缩放,压在框里是平移。
   *  只有这一层此刻在画面上才给手柄:摆看不见的素材是盲摆。 */
  const clipGripAt = useCallback((p: Pt): Grip | null => {
    if (!clipOnScreen || !clipRect) return null
    const r = clipRect
    const corners = [[r.x, r.y], [r.x + r.w, r.y], [r.x, r.y + r.h], [r.x + r.w, r.y + r.h]]
    for (const [cx, cy] of corners) {
      if (Math.abs(p.x - cx) <= HANDLE && Math.abs(p.y - cy) <= HANDLE) return 'clipScale'
    }
    if (p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h) return 'clipMove'
    return null
  }, [clipOnScreen, clipRect])

  const commitArrow = async (d: ArrowDraft) => {
    if (Math.hypot(d.to.x - d.from.x, d.to.y - d.from.y) < MIN_BOX) return
    const box = boxOf(d.from, d.to)
    const arrowBox = {
      x: Math.max(0, box.x - (box.w < MIN_BOX ? 0.5 : 0)),
      y: Math.max(0, box.y - (box.h < MIN_BOX ? 0.5 : 0)),
      w: Math.max(1, box.w),
      h: Math.max(1, box.h),
    }
    const points = [d.from, d.to].map((point) => ({
      x: Math.round((point.x / 100) * canvasW),
      y: Math.round((point.y / 100) * canvasH),
    }))
    try {
      await dispatch('annotation.add', {
        kind: 'arrow',
        label: label.trim() || t('mod.canvas.untitledLabel'),
        color: guideColor,
        bboxX: Math.round((arrowBox.x / 100) * canvasW),
        bboxY: Math.round((arrowBox.y / 100) * canvasH),
        bboxW: Math.round((arrowBox.w / 100) * canvasW),
        bboxH: Math.round((arrowBox.h / 100) * canvasH),
        pointsJson: JSON.stringify(points),
        start: Number(d.lo.toFixed(2)),
        duration: Number((d.hi - d.lo).toFixed(2)),
        targetClipId: top?.id,
      })
      setNote('')
    } catch (err: any) {
      setNote(String(err?.message || err))
    }
  }

  const onDown = (e: RPointerEvent) => {
    // 捕获只是为了指针划出画面时事件还回得来;它抛异常(指针已释放/合成事件)
    // 不该让整个手势作废 —— 手势本身只靠 from/to 两个点。
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* 无捕获也能画 */ }
    const p = ptOf(e)
    const at = studio.playhead

    if (studio.tool === 'select') {
      const hit = hitTest(p)
      if (hit) {
        setStudio({ picked: { type: 'annotation', id: hit.ann.id } })
        setDraft({ grip: hit.grip, annId: hit.ann.id, base: rectOf(hit.ann),
                   from: p, to: p, lo: at, hi: at })
        return
      }
      // 标注没命中,再看是不是在摆选中的那层素材。顺序不能反:标注在最上面,
      // 而这一层的框往往铺满整个画面 —— 先判素材的话标注就永远点不中了。
      const g = clipGripAt(p)
      if (g) { setDraft({ grip: g, from: p, to: p, lo: at, hi: at }); return }
      // 点空白 = 清选中,**什么都不建**。这一行就是「我他妈都没点触发标注」的答案。
      setStudio({ picked: null })
      return
    }
    if (!top) return                       // 这一刻没有画面,添加引导没有意义
    if (studio.tool === 'arrow') {
      const next = arrowPress(p, at)
      setArrowDraft(next)
      setStudio({ drafting: { start: next.lo, end: next.hi } })
      return
    }
    const guideKind: DrawGuideKind = studio.tool === 'text' ? 'text' : 'box'
    setDraft({ grip: 'draw', guideKind, from: p, to: p, lo: at, hi: at })
    setStudio({ drafting: { start: at, end: at } })   // 时间线当场把它高亮出来
  }

  const onMove = (e: RPointerEvent) => {
    if (arrowDraft) {
      setArrowDraft(arrowMove(arrowDraft, ptOf(e)))
      return
    }
    if (!draft) return
    setDraft({ ...draft, to: ptOf(e) })
  }

  /** 未松手滚轮 = 擦洗播放头,划过的时间自动成为区间(只在画新框时有意义)。 */
  const onWheel = (e: RWheelEvent) => {
    if (!arrowDraft && (!draft || draft.grip !== 'draw')) return
    const step = (e.shiftKey ? 1 : WHEEL_FRAMES) / FPS
    const next = clampTime(studio.playhead + (e.deltaY > 0 ? step : -step), duration)
    const active = arrowDraft || draft!
    const lo = Math.min(active.lo, next), hi = Math.max(active.hi, next)
    setStudio({ playhead: next, drafting: { start: lo, end: hi } })  // 实时,不等松手
    if (arrowDraft) setArrowDraft({ ...arrowDraft, lo, hi })
    else setDraft({ ...draft!, lo, hi })
  }

  const onUp = async (e?: RPointerEvent) => {
    if (arrowDraft) {
      const d = arrowDraft
      const end = e ? ptOf(e) : d.to
      const committed = arrowRelease(d, end, studio.playhead, MIN_BOX)
      setArrowDraft(null)
      setStudio({ drafting: null })
      if (committed) {
        await commitArrow(committed)
      } else {
        const hit = hitTest(d.from)
        if (hit && String(hit.ann.data?.kind || '') === 'arrow') {
          setStudio({ tool: 'select', picked: { type: 'annotation', id: hit.ann.id } })
        }
      }
      return
    }
    if (!draft) return
    const d = draft
    setDraft(null)
    setStudio({ drafting: null })

    // 摆素材:整段拖完只发一次。位移与缩放都是**比例**,所以换画布、换素材分辨率都不用重算。
    if (d.grip === 'clipMove' || d.grip === 'clipScale') {
      if (!pickedClip || !clipRect) return
      if (Math.abs(d.to.x - d.from.x) < 0.2 && Math.abs(d.to.y - d.from.y) < 0.2) return
      if (d.grip === 'clipMove') {
        const nx = num(pickedClip.data?.x) + (d.to.x - d.from.x) / 100
        const ny = num(pickedClip.data?.y) + (d.to.y - d.from.y) / 100
        await applyClip({ x: clampGeom(nx, 4), y: clampGeom(ny, 4) })
      } else {
        const base = pickedClip.data?.scale == null ? 1 : num(pickedClip.data.scale, 1)
        await applyClip({ scale: Math.max(0.05, Math.min(8, base * scaleFactor(clipRect, d))) })
      }
      return
    }

    if (d.grip === 'draw') {
      const box = boxOf(d.from, d.to)
      const kind = d.guideKind || 'box'
      if (kind === 'box' && (box.w < MIN_BOX || box.h < MIN_BOX)) return
      if (kind === 'text' && !label.trim()) {
        setNote(t('mod.canvas.needText'))
        return
      }
      const textBox = kind === 'text'
        ? { x: clamp100(d.from.x), y: clamp100(d.from.y), w: 28, h: 8 }
        : box
      await dispatch('annotation.add', {
        kind,
        label: label.trim() || t('mod.canvas.untitledLabel'),
        color: guideColor,
        bboxX: Math.round((textBox.x / 100) * canvasW),
        bboxY: Math.round((textBox.y / 100) * canvasH),
        bboxW: Math.round((textBox.w / 100) * canvasW),
        bboxH: Math.round((textBox.h / 100) * canvasH),
        start: Number(d.lo.toFixed(2)), duration: Number((d.hi - d.lo).toFixed(2)),
        targetClipId: top?.id,
      })
      setStudio({ tool: 'select' })                    // 落定即回 select
      setLabel('')
      setNote('')
      return
    }

    // 移动 / 改大小:整段拖完只发一次 entity.update
    const ann = annotations.find((a) => a.id === d.annId)
    if (!ann || !d.base) return
    // 只点一下没拖 = 只是选中。不加这个阈值就会落一条 before==after 的空 Operation:
    // undo 栈多一格、version +1,手快点两下第二下直接 cas_version_mismatch。
    if (Math.abs(d.to.x - d.from.x) < 0.2 && Math.abs(d.to.y - d.from.y) < 0.2) return
    const moved = movedBox(d)
    if (moved.w < MIN_BOX || moved.h < MIN_BOX) return
    try {
      const patches: Dict = {
        bboxX: Math.round((moved.x / 100) * canvasW),
        bboxY: Math.round((moved.y / 100) * canvasH),
        bboxW: Math.round((moved.w / 100) * canvasW),
        bboxH: Math.round((moved.h / 100) * canvasH),
      }
      if (String(ann.data?.kind || 'box') === 'arrow') {
        const old = d.base
        const transformed = arrowOf(ann, canvasW, canvasH).map((point) => ({
          x: Math.round(((moved.x + ((point.x - old.x) / Math.max(old.w, 0.001)) * moved.w)
            / 100) * canvasW),
          y: Math.round(((moved.y + ((point.y - old.y) / Math.max(old.h, 0.001)) * moved.h)
            / 100) * canvasH),
        }))
        patches.pointsJson = JSON.stringify(transformed)
      }
      if (['box', 'image'].includes(String(ann.data?.kind || 'box'))
          && keyframesFor(guideKeyframes, ann.id).length > 0) {
        await dispatch('guide.keyframe.set', {
          annotationId: ann.id,
          at: studio.playhead,
          ...patches,
        })
      } else {
        await dispatch('entity.update', {
          type: 'annotation', id: ann.id, expected_version: ann.version,
          patches,
        })
      }
    } catch (err: any) {
      setNote(String(err?.message || err))     // 版本冲突之类要说出来,不能静默吞掉
    }
  }

  /** 视频拖到画布上也能导入 —— 与时间线同一条路径,行为必须一模一样。
   *  dragCount 用计数不用布尔:进入子元素时会先收到子元素的 dragleave,
   *  用布尔会让提示一闪一闪。 */
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
    onDrop: async (e: React.DragEvent) => {
      e.preventDefault(); setDragCount(0)
      const files = Array.from(e.dataTransfer.files)
      const videos = files.filter(isVideoFile)
      const images = files.filter(isImageFile)
      if (!videos.length && !images.length) { setNote(t('mod.canvas.dropNothing')); return }
      try {
        if (videos.length) await importVideos(videos, { clips, dispatch, uploadAsset })
        if (images.length) {
          await importGuideImages(images, {
            clips, dispatch, uploadAsset, playhead: studio.playhead,
            duration, canvasW, canvasH,
          })
        }
        setNote('')
      } catch (err: any) {
        setNote(String(err?.message || err))
      }
    },
  }

  const togglePlay = () => {
    const el = videoRef.current
    if (!el) return
    if (el.paused) { wantPlay.current = true; void el.play() }
    else { wantPlay.current = false; el.pause() }
  }

  const activateTool = useCallback((tool: ToolId) => {
    setDraft(null)
    setArrowDraft(null)
    setNote('')
    setStudio({ ...toolActivationPatch(tool), drafting: null })
  }, [])

  /** 键盘:V/1 选择、R/2 框选、T/3 文字、A/4 路径、Esc 回选择、空格播放暂停。
   *  焦点在输入框里、或带 Cmd/Ctrl/Alt 时一律不接管。 */
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const el = ev.target as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || el?.isContentEditable) return
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return
      const k = ev.key.toLowerCase()
      const pick = (tool: ToolId) => {
        ev.preventDefault()
        activateTool(tool)
      }
      if (k === 'v' || k === '1') return pick('select')
      if (k === 'r' || k === '2') return pick('rect')
      if (k === 't' || k === '3') return pick('text')
      if (k === 'a' || k === '4') return pick('arrow')
      if (k === 'escape') { ev.preventDefault(); activateTool('select'); return }
      if (k === ' ') { ev.preventDefault(); togglePlay() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [activateTool])

  const span = arrowDraft
    ? { start: arrowDraft.lo, end: arrowDraft.hi }
    : draft?.grip === 'draw' ? { start: draft.lo, end: draft.hi } : studio.drafting
  const reading = span && span.end > span.start
    ? `${fmt(span.start)} – ${fmt(span.end)}`
    : t('mod.canvas.singleFrame')

  const drawing = draft?.grip === 'draw' ? boxOf(draft.from, draft.to) : null
  const drawingArrow = arrowDraft ? [arrowDraft.from, arrowDraft.to] : null
  const dragging = draft && draft.grip !== 'draw' ? movedBox(draft) : null

  const TOOLS: { id: ToolId; icon: string; num: number; labelKey: string }[] = [
    { id: 'select', icon: 'transform', num: 1, labelKey: 'mod.tool.select' },
    { id: 'rect', icon: 'rect', num: 2, labelKey: 'mod.tool.rect' },
    { id: 'text', icon: 'text', num: 3, labelKey: 'mod.tool.text' },
    { id: 'arrow', icon: 'points', num: 4, labelKey: 'mod.tool.arrow' },
  ]
  const hintKey = studio.tool === 'rect' ? 'mod.tool.rectHint'
    : studio.tool === 'text' ? 'mod.tool.textHint'
      : studio.tool === 'arrow' ? 'mod.tool.arrowHint' : 'mod.tool.selectHint'
  const colorControlMode = guideColorControlMode(
    studio.tool,
    pickedAnn ? String(pickedAnn.data?.kind || 'box') : null,
  )
  const colorControlValue = colorControlMode === 'selection'
    ? String(pickedAnn?.data?.color || DEFAULT_GUIDE_COLOR).toLowerCase()
    : guideColor

  const renderColorPicker = (
    selected: string,
    onChoose: (color: GuideColor) => void,
  ) => (
    <div className="ze-row" data-gap="0-5" role="group"
         aria-label={t('mod.tool.color')}>
      <span className="ze-text" data-role="tertiary">{t('mod.tool.color')}</span>
      {GUIDE_COLORS.map((item) => (
        <button key={item.id} type="button" className="ze-button" data-size="sm"
                data-variant={selected === item.value ? 'primary' : 'ghost'}
                aria-pressed={selected === item.value}
                aria-label={t(item.labelKey)}
                title={t(item.labelKey)}
                onClick={() => onChoose(item.value)}>
          <span aria-hidden="true">{item.glyph}</span>
        </button>
      ))}
    </div>
  )

  return (
    <div className="ze-stack" data-gap="0" {...dropProps}
         data-drop-active={dragCount > 0 ? 'true' : undefined}
         style={{ height: '100%', minHeight: 240 }}>
      {/* 顶栏:工具 + 随工具变的一句提示 + 实时读数 */}
      <div className="ze-bar ze-edge" data-edge="bottom">
        <div className="ze-row" data-gap="0-5" role="toolbar" aria-label={t('mod.tool.bar')}>
          {TOOLS.map((tool) => (
            <button key={tool.id} type="button" className="ze-icon-button" data-size="sm"
                    aria-pressed={studio.tool === tool.id}
                    aria-label={t(tool.labelKey)}
                    title={`${t(tool.labelKey)} (${tool.num})`}
                    onClick={() => activateTool(tool.id)}>
              <span className="ze-icon" data-icon={tool.icon} aria-hidden="true" />
            </button>
          ))}
          <label className="ze-button" data-size="sm" data-variant="ghost">
            {t('mod.tool.image')}
            <input type="file" accept="image/*" multiple style={{ display: 'none' }}
                   onChange={(event) => {
                     const files = Array.from(event.target.files || [])
                     event.currentTarget.value = ''
                     void importGuideImages(files, {
                       clips, dispatch, uploadAsset, playhead: studio.playhead,
                       duration, canvasW, canvasH,
                     }).catch((err) => setNote(String(err?.message || err)))
                   }} />
          </label>
          {colorControlMode
            ? renderColorPicker(
                colorControlValue,
                colorControlMode === 'selection'
                  ? (color) => void commitPickedColor(color)
                  : setGuideColor,
              )
            : null}
          {canAnimatePicked && pickedInRange ? (
            <div className="ze-row" data-gap="0-5" role="group"
                 aria-label={t('mod.keyframe.group')}>
              <button type="button" className="ze-button" data-size="sm"
                      data-variant={currentKeyframe ? 'primary' : 'ghost'}
                      onClick={() => void setPickedKeyframe()}>
                {t(currentKeyframe ? 'mod.keyframe.update' : 'mod.keyframe.add')}
              </button>
              {currentKeyframe ? (
                <button type="button" className="ze-button" data-size="sm"
                        data-variant="ghost"
                        onClick={() => void removeCurrentKeyframe()}>
                  {t('mod.keyframe.remove')}
                </button>
              ) : null}
              {pickedFrames.length ? (
                <span className="ze-text" data-role="tertiary">
                  {t('mod.keyframe.count', { n: pickedFrames.length })}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
        <span className="ze-text" data-role="tertiary">
          {t(hintKey)}
        </span>
        <span className="ze-spacer" />
        <span className="ze-text" data-role="tertiary" data-mono="true">
          {t('mod.canvas.at')} {fmt(studio.playhead)} · {t('mod.canvas.span')} {reading}
        </span>
      </div>

      {/* 画面(.ze-spatial = 框架的空间编辑视口原子) */}
      <div className="ze-spatial ze-fill"
           style={{ display: 'grid', placeItems: 'center', containerType: 'size' }}>
        {/* 舞台按项目画布比例居中,宽度取「装得下」的那一个(cqh = 本容器高度)。
            手势与标注都量在这个盒子上 —— 要是量在外层容器上,黑边也算进坐标,
            框就会画到画面外面去。 */}
        <div ref={surfaceRef} data-tool={studio.tool}
             onPointerDown={onDown} onPointerMove={onMove} onPointerUp={(e) => void onUp(e)}
             onWheel={onWheel}
             onDoubleClick={() => { if (studio.tool === 'select') togglePlay() }}
             style={{ position: 'relative', aspectRatio: `${canvasW} / ${canvasH}`,
                      width: `min(100%, calc(100cqh * ${canvasW} / ${canvasH}))`,
                      cursor: studio.tool === 'select' ? 'default' : 'crosshair',
                      touchAction: 'none' }}>
          {src
            ? <video ref={videoRef} src={src} muted playsInline preload="auto"
                     className="ze-spatial__img"
                     onLoadedMetadata={(e) => {
                       const v = e.currentTarget
                       if (v.videoWidth > 0) setNatural({ w: v.videoWidth, h: v.videoHeight })
                     }}
                     onError={() => {
                       // If the retained ticket eventually expires before the browser has all
                       // required ranges, retry with the newest ticket without periodic reloads.
                       if (sourceCandidate.url && sourceCandidate.url !== src) {
                         setStableSource(sourceCandidate)
                       }
                     }}
                     style={{ width: '100%', height: '100%', pointerEvents: 'none',
                              ...topGeomStyle }} />
            : <div className="ze-canvas__empty">
                {t(clips.length ? 'mod.canvas.noLayer' : 'mod.canvas.emptyDropHint')}
              </div>}
          {shown.filter((guide) => String(guide.data?.kind || '') === 'image').map((guide) => {
            const live = dragging && draft?.annId === guide.id ? dragging : rectOf(guide)
            const url = guideImageUrl(guide)
            return url ? (
              <img key={`image-${guide.id}`} src={url} alt=""
                   style={{ position: 'absolute', pointerEvents: 'none',
                            left: `${live.x}%`, top: `${live.y}%`,
                            width: `${live.w}%`, height: `${live.h}%`,
                            objectFit: 'contain' }} />
            ) : null
          })}
          {dragCount > 0 && (
            <div className="ze-spatial__hud">
              <span className="ze-text" data-role="secondary">{t('mod.canvas.dropHere')}</span>
            </div>
          )}

          {/* 标注图层 + 正在拉的框:一张屏幕空间 SVG,描边全归 L2 原子 */}
          <svg className="ze-spatial__overlay" viewBox="0 0 100 100" preserveAspectRatio="none">
            {/* 选中视频图层时的变换框 —— 画在标注下面,免得挡住框的角手柄 */}
            {clipOnScreen && clipLive && (
              <g>
                <rect className="ze-spatial__box" data-selected="true"
                      vectorEffect="non-scaling-stroke"
                      x={clipLive.x} y={clipLive.y} width={clipLive.w} height={clipLive.h} />
                {[[clipLive.x, clipLive.y], [clipLive.x + clipLive.w, clipLive.y],
                  [clipLive.x, clipLive.y + clipLive.h],
                  [clipLive.x + clipLive.w, clipLive.y + clipLive.h]].map(([cx, cy], i) => (
                  <rect key={i} className="ze-gizmo__handle" vectorEffect="non-scaling-stroke"
                        x={cx - 0.9} y={cy - 0.9} width={1.8} height={1.8} />
                ))}
              </g>
            )}
            {shown.map((a) => {
              const live = dragging && draft?.annId === a.id ? dragging : rectOf(a)
              const picked = studio.picked?.type === 'annotation' && studio.picked.id === a.id
              const kind = String(a.data?.kind || 'box')
              const stroke = String(a.data?.color || DEFAULT_GUIDE_COLOR)
              if (kind === 'arrow') {
                const points = arrowOf(a, canvasW, canvasH)
                const path = points.map((p) => `${p.x},${p.y}`).join(' ')
                return (
                  <g key={a.id}>
                    <polyline className="ze-spatial__box"
                              data-selected={picked ? 'true' : undefined}
                              vectorEffect="non-scaling-stroke" points={path}
                              style={{ stroke }} />
                    {arrowHead(points).map(([from, to], i) => (
                      <line key={i} className="ze-spatial__box"
                            data-selected={picked ? 'true' : undefined}
                            vectorEffect="non-scaling-stroke"
                            style={{ stroke }}
                            x1={from.x} y1={from.y} x2={to.x} y2={to.y} />
                    ))}
                  </g>
                )
              }
              return (
                <g key={a.id}>
                  <rect className="ze-spatial__box" data-selected={picked ? 'true' : undefined}
                        vectorEffect="non-scaling-stroke"
                        style={{ stroke }}
                        x={live.x} y={live.y} width={live.w} height={live.h} />
                  {kind === 'text' && (
                    <text className="ze-text" data-role="secondary"
                          x={live.x + 1} y={live.y + 4}
                          fontSize={3.2} fill="currentColor">
                      {String(a.data?.label || '')}
                    </text>
                  )}
                  {picked && [[live.x, live.y], [live.x + live.w, live.y],
                              [live.x, live.y + live.h],
                              [live.x + live.w, live.y + live.h]].map(([cx, cy], i) => (
                    <rect key={i} className="ze-gizmo__handle" vectorEffect="non-scaling-stroke"
                          x={cx - 0.9} y={cy - 0.9} width={1.8} height={1.8} />
                  ))}
                </g>
              )
            })}
            {drawing && (
              <rect className="ze-spatial__marquee" vectorEffect="non-scaling-stroke"
                    style={{ stroke: guideColor }}
                    x={drawing.x} y={drawing.y} width={drawing.w} height={drawing.h} />
            )}
            {drawingArrow && (
              <g>
                <line className="ze-spatial__marquee" vectorEffect="non-scaling-stroke"
                      style={{ stroke: guideColor }}
                      x1={drawingArrow[0].x} y1={drawingArrow[0].y}
                      x2={drawingArrow[1].x} y2={drawingArrow[1].y} />
                {arrowHead(drawingArrow).map(([from, to], i) => (
                  <line key={i} className="ze-spatial__marquee"
                        vectorEffect="non-scaling-stroke" style={{ stroke: guideColor }}
                        x1={from.x} y1={from.y} x2={to.x} y2={to.y} />
                ))}
              </g>
            )}
          </svg>

          {shown.length > 0 && (
            <div className="ze-spatial__hud">
              <span className="ze-text" data-role="tertiary">
                {t('mod.canvas.marksHere', { n: shown.length })}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* 底栏随选中对象换内容(与「选中标注 → 改文字」同一条上下文栏范式):
          选中视频图层 → 摆它进画布;其余 → 写这一句要改成什么。 */}
      {pickedClip ? (
        <div className="ze-bar ze-edge" data-edge="top">
          <span className="ze-text" data-role="tertiary" data-truncate="true">
            {String(pickedClip.name || '')}
          </span>
          <div className="ze-row" data-gap="0-5">
            {FITS.map((f) => (
              <button key={f} type="button" className="ze-button" data-size="sm"
                      data-variant={String(pickedClip.data?.fitMode || 'contain') === f
                        ? 'primary' : 'ghost'}
                      onClick={() => void applyClip({ fitMode: f })}>
                {t(`mod.canvas.fit.${f}`)}
              </button>
            ))}
          </div>
          <button type="button" className="ze-button" data-size="sm"
                  disabled={isIdentity(pickedClip.data as any)}
                  onClick={() => void applyClip({ fitMode: 'contain', x: 0, y: 0, scale: 1,
                                                  cropL: 0, cropT: 0, cropR: 0, cropB: 0 })}>
            {t('mod.canvas.resetGeom')}
          </button>
          <span className="ze-spacer" />
          <span className="ze-text" data-role="tertiary">
            {t(clipOnScreen ? 'mod.canvas.clipHint' : 'mod.canvas.clipOffscreen')}
          </span>
        </div>
      ) : (
        /* **失焦 / 回车才提交**,不逐字符提交 —— 否则一句话打进去就吃掉几十格 undo。 */
        <div className="ze-bar ze-edge" data-edge="top">
          <input ref={pickedAnn ? undefined : creationInputRef} className="ze-input"
                 value={pickedAnn ? labelDraft : label}
                 placeholder={t(pickedAnn ? 'mod.canvas.labelEdit' : 'mod.canvas.labelPlaceholder')}
                 onChange={(e) => (pickedAnn ? setLabelDraft(e.target.value) : setLabel(e.target.value))}
                 onBlur={() => void commitLabel()}
                 onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void commitLabel() } }} />
          <button type="button" className="ze-button" data-size="sm" onClick={togglePlay}>
            {t('mod.canvas.playPause')}
          </button>
        </div>
      )}

      {note && (
        <div className="ze-bar">
          <span className="ze-text" data-role="tertiary" data-tone="danger">{note}</span>
        </div>
      )}
    </div>
  )
}

/** 拉角缩放的倍率:指针离框心的距离 ÷ 那个角原本离框心的距离。
 *  用欧氏距离而不是单轴,四个角手感一致;贴到框心时给个下限,免得除出 0 或负数。 */
function scaleFactor(rect: Box, d: { from: Pt; to: Pt }): number {
  const cx = rect.x + rect.w / 2, cy = rect.y + rect.h / 2
  const was = Math.hypot(d.from.x - cx, d.from.y - cy)
  const now = Math.hypot(d.to.x - cx, d.to.y - cy)
  if (was < 0.5) return 1
  return Math.max(0.05, Math.min(20, now / was))
}

/** 几何字段夹到 schema 边界内 —— 拖出界就被后端拒,人只会看到一句红字。 */
function clampGeom(v: number, limit: number): number {
  return Math.round(Math.max(-limit, Math.min(limit, v)) * 10000) / 10000
}
