/**
 * TextZone — 文本内容区(functional zone;SPEC §2B `text`)
 *
 * 固定骨架(组件画死):body 容器 + 运行态骨架/错误占位 + 受控 textarea;
 * `variant='code'` 加行号 gutter(等宽);`preview` 态(text)渲染只读 HTML(由编辑器算好传入)。
 * 复用现 `.ze-text__* / .ze-code__*`(text.css/code.css;CSS 暂留 L4,Phase 4 再去令牌)。
 * 受控:value 进、onChange(输入)/ onCommit(blur 写回)出 —— 草稿与写回逻辑留在编辑器。
 */

import { type ReactNode } from 'react'
import { ErrorPlaceholder, SkeletonLines } from '../lib/Skeleton'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import type { RunError } from '../types'

export type TextZoneProps = {
  value: string
  onChange: (v: string) => void
  onCommit: () => void
  readOnly?: boolean
  /** 运行态:error → 错误占位;pending/running → 骨架;其余 → 可编辑。 */
  phase?: string
  error?: RunError
  placeholder?: string
  /** 'code' 加行号 gutter + 等宽;'text'(缺省)纯多行文本。 */
  variant?: 'text' | 'code'
  /** text 预览态:渲染 previewHtml(只读);编辑器负责算 HTML。 */
  preview?: boolean
  previewHtml?: string
}

export function TextZone({
  value, onChange, onCommit, readOnly, phase, error, placeholder,
  variant = 'text', preview, previewHtml,
}: TextZoneProps): ReactNode {
  const isCode = variant === 'code'
  const previewRef = useScrollAffordance('y')

  let inner: ReactNode
  if (phase === 'error') {
    inner = <ErrorPlaceholder error={error} />
  } else if (phase === 'pending' || phase === 'running') {
    inner = <SkeletonLines lines={6} />
  } else if (isCode) {
    const lineCount = value.length ? value.split('\n').length : 1
    inner = (
      <div className="ze-code__edit ze-surface" data-surface="sunken">
        <pre className="ze-code__gutter ze-text ze-surface ze-edge ze-pad" data-role="tertiary" data-mono="true" data-leading="normal" data-surface="raised" data-edge="right" data-p="2" aria-hidden="true">
          {Array.from({ length: lineCount }, (_, i) => i + 1).join('\n')}
        </pre>
        <textarea
          className="ze-code__textarea ze-text ze-pad" data-mono="true" data-size="xs" data-leading="normal" data-p="2"
          value={value}
          spellCheck={false}
          disabled={readOnly}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onCommit}
          placeholder={placeholder}
        />
      </div>
    )
  } else if (preview) {
    inner = <div ref={previewRef} className="ze-text__preview ze-scroll-fade-y ze-text" data-prose="true" dangerouslySetInnerHTML={{ __html: previewHtml ?? '' }} />
  } else {
    inner = (
      <textarea
        className="ze-textarea ze-text__textarea" data-size="sm"
        value={value}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onCommit}
        placeholder={placeholder}
      />
    )
  }

  return isCode
    ? <div className="ze-code__body" data-run-phase={phase}>{inner}</div>
    : <div className="ze-text__body ze-pad" data-p="3" data-run-phase={phase}>{inner}</div>
}
