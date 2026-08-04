/**
 * CompareFrame — A/B 前后对比图元(W4;opt-in primitive)
 *
 * 三态:并排(side-by-side)/ 滑块分隔(slider)/ 叠加(onion,不透明可调)。纯展示,
 * 供 mod 的 review slot 用(原图 vs 采纳的 candidate)。几何/约束纯逻辑在 compareGeometry.ts(已单测)。
 * 视觉:绝对严格分层(P4)—— compare.css 只留无令牌结构,令牌视觉全由本组件贴 L2 原子
 * (.ze-row/.ze-grid/.ze-stack/.ze-pad/.ze-edge/.ze-surface/.ze-round/.ze-size/.ze-inset/
 * .ze-elevate/.ze-text)。描边强调色走 currentColor(divider 贴 .ze-text[data-tone=accent])。
 */

import { useCallback, useRef, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { Slider } from './Slider'
import { COMPARE_MODES, afterClipInset, pointerToSplit, type CompareMode } from './compareGeometry'

export type CompareFrameProps = {
  beforeUrl?: string
  afterUrl?: string
  beforeLabel?: string
  afterLabel?: string
  mode?: CompareMode
  /** slider 初始分隔(0..1) */
  initialSplit?: number
  /** onion 初始不透明(0..1) */
  initialOpacity?: number
}

export function CompareFrame({
  beforeUrl, afterUrl, beforeLabel, afterLabel,
  mode: initialMode = 'slider', initialSplit = 0.5, initialOpacity = 0.5,
}: CompareFrameProps): ReactNode {
  const t = useT()
  const [mode, setMode] = useState<CompareMode>(initialMode)
  const [split, setSplit] = useState(initialSplit)
  const [opacity, setOpacity] = useState(initialOpacity)
  const stageRef = useRef<HTMLDivElement>(null)
  const sliding = useRef(false)

  const bLabel = beforeLabel ?? t('editor.compareBefore')
  const aLabel = afterLabel ?? t('editor.compareAfter')

  const modeLabel = (mid: CompareMode): string =>
    mid === 'side-by-side' ? t('editor.compareModeSideBySide')
      : mid === 'slider' ? t('editor.compareModeSlider')
        : t('editor.compareModeOnion')

  const updateSplit = useCallback((clientX: number) => {
    const el = stageRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    setSplit(pointerToSplit(clientX - rect.left, rect.width))
  }, [])

  const onDividerDown = useCallback((e: React.PointerEvent) => {
    sliding.current = true
    e.currentTarget.setPointerCapture(e.pointerId)
    updateSplit(e.clientX)
  }, [updateSplit])

  const onDividerMove = useCallback((e: React.PointerEvent) => {
    if (sliding.current) updateSplit(e.clientX)
  }, [updateSplit])

  const onDividerUp = useCallback((e: React.PointerEvent) => {
    sliding.current = false
    try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
  }, [])

  return (
    <div className="ze-compare" data-mode={mode}>
      <div className="ze-compare__toolbar ze-row ze-pad ze-edge" data-p="2" data-edge="bottom">
        {COMPARE_MODES.map((mid) => (
          <button
            key={mid} type="button" className="ze-button" data-size="sm"
            data-variant={mode === mid ? 'solid' : 'ghost'}
            aria-pressed={mode === mid}
            onClick={() => setMode(mid)}
          >{modeLabel(mid)}</button>
        ))}
        {mode === 'onion' && (
          <span className="ze-compare__opacity ze-gap" data-gap="2">
            <span className="ze-compare__opacity-label ze-text" data-role="secondary" data-size="xs">{aLabel}</span>
            <Slider value={opacity} min={0} max={1} step={0.01} onChange={setOpacity} />
          </span>
        )}
      </div>

      {mode === 'side-by-side' ? (
        <div className="ze-compare__pair ze-grid ze-pad" data-cols="2" data-gap="2" data-p="2">
          <figure className="ze-compare__cell ze-stack" data-gap="1" data-align="center">
            {beforeUrl && <img className="ze-compare__img ze-surface" data-surface="sunken" src={beforeUrl} alt="" draggable={false} />}
            <figcaption className="ze-text" data-role="secondary" data-size="xs">{bLabel}</figcaption>
          </figure>
          <figure className="ze-compare__cell ze-stack" data-gap="1" data-align="center">
            {afterUrl && <img className="ze-compare__img ze-surface" data-surface="sunken" src={afterUrl} alt="" draggable={false} />}
            <figcaption className="ze-text" data-role="secondary" data-size="xs">{aLabel}</figcaption>
          </figure>
        </div>
      ) : (
        <div className="ze-compare__stage ze-surface" data-surface="sunken" ref={stageRef}>
          {beforeUrl && <img className="ze-compare__layer" src={beforeUrl} alt="" draggable={false} />}
          {afterUrl && (
            <img
              className="ze-compare__layer ze-compare__layer--after"
              src={afterUrl} alt="" draggable={false}
              style={mode === 'slider'
                ? { clipPath: afterClipInset(split) }
                : { opacity }}
            />
          )}
          {mode === 'slider' && (
            <div
              className="ze-compare__divider ze-text"
              data-tone="accent"
              style={{ left: `${split * 100}%` }}
              onPointerDown={onDividerDown}
              onPointerMove={onDividerMove}
              onPointerUp={onDividerUp}
              onPointerCancel={onDividerUp}
            >
              <span
                className="ze-compare__handle ze-size ze-round ze-surface ze-elevate"
                data-w="6" data-h="6" data-radius="pill" data-surface="raised"
                aria-hidden="true"
              />
            </div>
          )}
          <span
            className="ze-compare__tag ze-inset ze-pad ze-text ze-surface ze-round"
            data-top="2" data-left="2" data-py="0-5" data-px="2"
            data-size="xs" data-tone="inverse" data-surface="strong" data-radius="sm"
          >{bLabel}</span>
          <span
            className="ze-compare__tag ze-inset ze-pad ze-text ze-surface ze-round"
            data-top="2" data-right="2" data-py="0-5" data-px="2"
            data-size="xs" data-tone="inverse" data-surface="strong" data-radius="sm"
          >{aLabel}</span>
        </div>
      )}
    </div>
  )
}
