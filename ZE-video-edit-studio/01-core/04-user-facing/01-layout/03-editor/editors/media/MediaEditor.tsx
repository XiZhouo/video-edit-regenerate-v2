/**
 * MediaEditor — `media` editor class:视频 / 音频播放(自定义传输条)。
 *
 * 按 asset_ref.mime 选 <video> 还是 <audio>;src 经 assetUrl 解析。运行态占位同其他 editor。
 *
 * zone 化(P3③b):外壳=`EditorFrame`;操作条=`ToolbarZone`(仅 title);内容=`PreviewZone`
 * (player/empty/媒体加载错误 + busy 覆盖层)+ `TransportZone`(传输条)。**错误两类**:entity
 * 运行错误(phase==='error')交 PreviewZone 统一渲染;**媒体加载错误**(`loadErr`,非 phase error)
 * 作为 PreviewZone 的 children 内联渲染,可点重试清错。传输/scrub/倍速/循环走原生 media API
 * (经 ref,不 dispatch);标记 in/out 段 → markCommand(mod 声明,走 dispatch)。
 *
 * 共享时间游标(C0,首个真实消费者):若装配层注入 `currentTime`/`setCurrentTime`(经 SlotContext,
 * = 与 selection 同级的 Interaction State 轴),media 双向接通 —— (a) 播放/scrub 的 timeUpdate 把
 * 原生 currentTime 「提升」为共享 currentTime;(b) 别处改了 currentTime 则把 video seek 过去。
 * 防回环靠 `shouldSeek`(仅当差值超阈值才 seek,吸收同帧回灌)。未注入则退回纯本地播放,行为不变。
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { shouldSeek, DEFAULT_SEEK_EPSILON } from '../../lib/playhead'
import { ErrorPlaceholder } from '../../lib/Skeleton'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { PreviewZone } from '../../zones/PreviewZone'
import { TransportZone } from '../../zones/TransportZone'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type MediaEditorProps = {
  entity: EntityDict | null
  /** 编辑器名(layout slot.nameKey 译名;优先显示槽位名,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  assetUrl?: AssetUrlFn
  status?: EntityRunStatus
  mediaField?: string
  /** 逐帧步长用的帧率(视频);默认 30 */
  fps?: number
  /** 标记 in/out 命令 id(mod 声明,如 `media.mark {type,id,in,out}`);无则不显示标记按钮 */
  markCommand?: string
  dispatch?: DispatchFn
  /** 共享时间游标(秒,C0):外部(别的面板/transport)写它 → media seek 过去(防回环)。未传=不联动。 */
  currentTime?: number
  /** 把 media 播放/scrub 的时间提升为共享游标;timeUpdate / seek 时调。未传=不向外联动。 */
  onCurrentTimeChange?: (t: number) => void
}

const RATES = [0.5, 1, 1.5, 2] as const

function pickRef(data: Record<string, unknown>, field?: string): AssetRefDict | null {
  const asRef = (v: unknown) => (v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? v as AssetRefDict : null)
  if (field) return asRef(data[field])
  for (const v of Object.values(data)) { const r = asRef(v); if (r) return r }
  return null
}

export function MediaEditor({ entity, editorName, assetUrl, status, mediaField, fps = 30, markCommand, dispatch, currentTime, onCurrentTimeChange }: MediaEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const mediaRef = useRef<HTMLVideoElement & HTMLAudioElement>(null)
  // 上次向外发布的共享游标(防 timeUpdate ~4Hz 高频 setCurrentTime 灌爆订阅者:差值在阈内不发)。
  const lastPushedRef = useRef<number | null>(null)
  const [loadErr, setLoadErr] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [cur, setCur] = useState(0)
  const [dur, setDur] = useState(0)
  const [rate, setRate] = useState(1)
  const [loop, setLoop] = useState(false)
  const [markIn, setMarkIn] = useState<number | null>(null)
  const [markOut, setMarkOut] = useState<number | null>(null)

  const ref = entity ? pickRef(entity.data, mediaField) : null
  const src = ref && assetUrl ? assetUrl(ref) : undefined
  // src 变了重置一切播放/标记/倍速/循环态(新 <video>/<audio> 元素默认 rate=1 / loop=false,状态须跟随)
  useEffect(() => {
    setLoadErr(false); setPlaying(false); setCur(0); setDur(0)
    setRate(1); setLoop(false); setMarkIn(null); setMarkOut(null)
    lastPushedRef.current = null
  }, [src])

  // 重新进入生成中 → 清掉上一次的媒体加载错误,让"处理中"覆盖层显示(而非粘住旧错误)
  const _phase = status?.phase
  useEffect(() => {
    if (_phase === 'pending' || _phase === 'running') setLoadErr(false)
  }, [_phase])

  // 共享游标向内读(C0):外部 currentTime 变(别的面板拖了 playhead)→ media seek 过去。
  // 防回环:仅当与媒体元素实际时间差超阈值才 seek(shouldSeek 吸收 timeUpdate↔seek 同帧回灌);
  // 这里直写元素 + 本地 cur,**不**调 onCurrentTimeChange(否则与外部 setter 互相喂回)。
  useEffect(() => {
    if (currentTime === undefined) return
    const m = mediaRef.current
    if (!m || !shouldSeek(currentTime, m.currentTime)) return
    m.currentTime = currentTime
    setCur(currentTime)
    // 记成已发布值:seek 落定后紧随的 timeUpdate 不把同一时间再发回外部(冗余回灌)。
    lastPushedRef.current = currentTime
  }, [currentTime])

  if (!entity) {
    return (
      <EditorFrame editorClass="media" header={<HeaderZone title={editorName ?? t('editor.noMedia')} />}>
        <EditorFrame.Empty>{t('editor.noMedia')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }
  const phase = status?.phase
  const isAudio = ref?.mime?.startsWith('audio/')
  const retryStatus = status?.run_id
    ? () => fire('pipeline.retry_failed', { pipeline_id: status.run_id, item_ids: [entity.id] })
    : undefined

  const el = () => mediaRef.current
  const togglePlay = () => { const m = el(); if (!m) return; if (m.paused) void m.play(); else m.pause() }
  // 本地时间变(scrub/逐帧/播放)→ 本地 state + 提升为共享游标(C0 向外写);未注入回调则纯本地。
  // 向外发布前按阈值去抖:timeUpdate ~4Hz,差值在阈内的回灌不发,避免无谓灌爆 currentTime 订阅者。
  const pushTime = (v: number) => {
    setCur(v)
    if (!onCurrentTimeChange) return
    const last = lastPushedRef.current
    if (last !== null && Math.abs(v - last) <= DEFAULT_SEEK_EPSILON) return
    lastPushedRef.current = v
    onCurrentTimeChange(v)
  }
  const seek = (v: number) => { const m = el(); if (!m) return; m.currentTime = v; pushTime(v) }
  const stepFrame = (dir: 1 | -1) => { const m = el(); if (!m) return; m.pause(); seek(Math.max(0, Math.min(dur, m.currentTime + dir / fps))) }
  const changeRate = (r: number) => { const m = el(); if (!m) return; m.playbackRate = r; setRate(r) }
  const toggleLoop = () => { const m = el(); if (!m) return; m.loop = !m.loop; setLoop(m.loop) }

  const fireMark = (nin: number | null, nout: number | null) => {
    if (markCommand) fire(markCommand, { type: entity.type, id: entity.id, in: nin, out: nout, expected_version: entity.version })
  }
  const setIn = () => { const v = el()?.currentTime ?? cur; setMarkIn(v); fireMark(v, markOut) }
  const setOut = () => { const v = el()?.currentTime ?? cur; setMarkOut(v); fireMark(markIn, v) }

  const region = markIn !== null && markOut !== null && dur > 0
    ? { left: `${(Math.min(markIn, markOut) / dur) * 100}%`, width: `${(Math.abs(markOut - markIn) / dur) * 100}%` }
    : null

  const showControls = !!src && !loadErr && phase !== 'pending' && phase !== 'running' && phase !== 'error'

  // 内容区:entity 运行错误交 PreviewZone(phase==='error');媒体加载错误(loadErr,非 phase error)
  // 与空态/播放器一样作 children 内联渲染。busy 覆盖层由 PreviewZone 统一挂(CanvasBusyOverlay 自判)。
  const body: ReactNode = loadErr ? (
    <ErrorPlaceholder error={{ code: 'media_load', message: t('editor.genFailed') }} onRetry={() => setLoadErr(false)} />
  ) : !src ? (
    <div className="ze-editor__empty">{t('editor.mediaEmpty')}</div>
  ) : (
    <div className="ze-media__player" data-audio={isAudio ? 'true' : undefined}>
      {isAudio ? (
        <audio ref={mediaRef} className="ze-media__el ze-surface" data-surface="inverse" src={src} onError={() => setLoadErr(true)}
          onLoadedMetadata={(e) => setDur(e.currentTarget.duration)}
          onTimeUpdate={(e) => pushTime(e.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} />
      ) : (
        <video ref={mediaRef} className="ze-media__el ze-surface" data-surface="inverse" src={src} onError={() => setLoadErr(true)}
          onClick={togglePlay}
          onLoadedMetadata={(e) => setDur(e.currentTarget.duration)}
          onTimeUpdate={(e) => pushTime(e.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} />
      )}
    </div>
  )

  const objectName = entity.name || entity.id.slice(0, 8)
  const headerTitle = editorName ? `${editorName} · ${objectName}` : objectName
  return (
    <EditorFrame
      editorClass="media"
      header={<HeaderZone title={headerTitle} />}
      zones={showControls ? [
        <TransportZone
          key="transport"
          playing={playing} current={cur} duration={dur} rate={rate} loop={loop}
          region={region} isAudio={isAudio} rates={RATES} showMarks={!!markCommand}
          onTogglePlay={togglePlay} onSeek={seek} onStepFrame={stepFrame} onRate={changeRate}
          onToggleLoop={toggleLoop} onMarkIn={setIn} onMarkOut={setOut}
        />,
      ] : undefined}
    >
      <PreviewZone phase={phase} error={status?.error} onRetry={retryStatus} busyStatus={status}>
        {body}
      </PreviewZone>
    </EditorFrame>
  )
}
