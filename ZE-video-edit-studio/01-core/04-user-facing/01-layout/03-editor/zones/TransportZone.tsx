/**
 * TransportZone — 媒体传输条(操作区 zone;SPEC §2 `transport`)
 *
 * 固定骨架(组件画死):播放/暂停 + 逐帧(视频)+ scrubber(带标记段叠加)+ 时码 + 倍速 +
 * 循环 + 标记 in/out。**这些是原生 media API 的视图控制(经 ref,不 dispatch)** —— 与一般
 * zone「拓展点只收命令/字段映射」不同,媒体播放本质是命令式(ref 驱动),故 transport 的拓展点
 * 是一组**播放回调**(编辑器持 mediaRef + 播放态,把 handler 传入)。标记 in/out 才是领域写,
 * 编辑器在 `onMarkIn/onMarkOut` 里走 dispatch。CSS 复用现 `.ze-media__controls`(L4,P4 去令牌)。
 */

import { type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'
import { useScrollAffordance } from '../lib/useScrollAffordance'

function clock(s: number): string {
  if (!Number.isFinite(s)) return '0:00'
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}

export type TransportZoneProps = {
  playing: boolean
  current: number
  duration: number
  rate: number
  loop: boolean
  /** 标记段在进度条上的叠加位置(编辑器按 markIn/markOut/duration 算好);无标记则 null。 */
  region: { left: string; width: string } | null
  /** 音频隐藏逐帧(无帧概念)。 */
  isAudio?: boolean
  /** 倍速可选值(闭集枚举)。 */
  rates: readonly number[]
  /** 是否显示标记 in/out 段(mod 声明 markCommand 时为 true)。 */
  showMarks?: boolean
  onTogglePlay: () => void
  onSeek: (v: number) => void
  onStepFrame: (dir: 1 | -1) => void
  onRate: (r: number) => void
  onToggleLoop: () => void
  onMarkIn: () => void
  onMarkOut: () => void
}

export function TransportZone({
  playing, current, duration, rate, loop, region, isAudio, rates, showMarks,
  onTogglePlay, onSeek, onStepFrame, onRate, onToggleLoop, onMarkIn, onMarkOut,
}: TransportZoneProps): ReactNode {
  const t = useT()
  const hugRef = useScrollAffordance('x')
  return (
    <div ref={hugRef} className="ze-media__controls ze-editor__zone" data-zone="transport" data-role="transport" data-lines="1" data-layout="hug-scroll">
      <button type="button" className="ze-icon-button" data-size="sm"
        title={playing ? t('editor.pause') : t('editor.play')} onClick={onTogglePlay}>{playing ? <ZeIcon name="pause" /> : <ZeIcon name="play" />}</button>
      {!isAudio && (
        <>
          <button type="button" className="ze-icon-button" data-size="sm" title={t('editor.frameBack')} onClick={() => onStepFrame(-1)}><ZeIcon name="step-prev" /></button>
          <button type="button" className="ze-icon-button" data-size="sm" title={t('editor.frameFwd')} onClick={() => onStepFrame(1)}><ZeIcon name="step-next" /></button>
        </>
      )}
      <div className="ze-media__scrubber-wrap">
        {region && <span className="ze-media__mark-region ze-surface ze-round" data-surface="accent-wash" data-radius="pill" style={region} aria-hidden="true" />}
        <input className="ze-slider ze-media__scrubber" type="range" min={0} max={duration || 0} step="any"
          value={current} onChange={(e) => onSeek(Number(e.target.value))} aria-label={t('editor.scrubber')} />
      </div>
      <span className="ze-media__time ze-text" data-role="tertiary" data-mono="true">{clock(current)} / {clock(duration)}</span>
      <span className="ze-select" data-size="sm" title={t('editor.rate')}>
        <select className="ze-select__control" aria-label={t('editor.rate')} value={rate} onChange={(e) => onRate(Number(e.target.value))}>
          {rates.map((r) => <option key={r} value={r}>{r}×</option>)}
        </select>
      </span>
      <button type="button" className="ze-icon-button" data-size="sm" data-variant={loop ? 'primary' : undefined}
        title={t('editor.loop')} onClick={onToggleLoop}><ZeIcon name="loop" /></button>
      {showMarks && (
        <span className="ze-media__marks ze-row" data-gap="1">
          <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={onMarkIn}>{t('editor.markIn')}</button>
          <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={onMarkOut}>{t('editor.markOut')}</button>
        </span>
      )}
    </div>
  )
}
