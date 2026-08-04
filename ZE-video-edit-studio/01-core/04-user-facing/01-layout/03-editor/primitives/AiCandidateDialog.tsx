/**
 * AiCandidateDialog — 「弹窗 → 一句话 → AI 设计 → candidate 预览 → 采纳」打包模式(F4)。
 *
 * 纯受控展示件:core 只打包交互模式(ze-dialog 原子弹窗壳 + 意图输入 + busy/error 态 +
 * candidate kv 预览 + 采纳/取消),**不碰 AI 通道也不解释 candidate 语义**——
 * mod 负责:onSend 派发自家设计命令(stage/native 自便)、candidate/busy/error 从自家
 * 实体字段派生(SSE 回流驱动重渲染)、onAdopt 派发采纳命令。数据流全在 mod,
 * 与 CONSUMPTION 哲学一致(core 出机制,mod 出策略)。
 *
 * 消费(典型):
 *   {open && <AiCandidateDialog title={t('mod.x.title')} busy={running} error={err}
 *     candidate={draft} previewFields={['title','severity']} fieldLabels={labels}
 *     onSend={(intent) => dispatch('X.DesignWithAi', { intent })}
 *     onAdopt={() => dispatch('X.AdoptCandidate', {})} onClose={close} />}
 *
 * 弹窗关闭不取消后台设计(状态在 mod 实体上,重开弹窗即恢复现场)。
 */

import { useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useScrollAffordance } from '../lib/useScrollAffordance'

export type AiCandidateDialogProps = {
  /** 弹窗标题(业务文案,mod 传译文)。 */
  title: string
  /** 意图输入占位文案(业务文案)。 */
  placeholder?: string
  /** 设计进行中(输入/发送/采纳禁用,显示思考中行)。 */
  busy?: boolean
  /** 设计失败信息(danger 行;null/空 = 无错误)。 */
  error?: string | null
  /** AI 产出的候选草稿(mod 从自家实体字段读出;null = 尚无候选)。 */
  candidate?: Record<string, unknown> | null
  /** 候选附言(AI 对设计的说明,可选)。 */
  note?: string | null
  /** 预览字段白名单 + 顺序;缺省 = candidate 全部键。 */
  previewFields?: string[]
  /** 字段显示名(mod 传译文;缺键回退字段名)。 */
  fieldLabels?: Record<string, string>
  onSend: (intent: string) => void
  onAdopt: () => void
  onClose: () => void
}

function previewValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (Array.isArray(value)) return value.map(previewValue).join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

export function AiCandidateDialog(props: AiCandidateDialogProps): ReactNode {
  const { title, placeholder, busy, error, candidate, note, previewFields, fieldLabels, onSend, onAdopt, onClose } = props
  const t = useT()
  const [intent, setIntent] = useState('')
  const fadeRef = useScrollAffordance('y')

  const fields = candidate
    ? (previewFields && previewFields.length ? previewFields : Object.keys(candidate)).filter((k) => k in candidate)
    : []
  const send = () => {
    const text = intent.trim()
    if (!text || busy) return
    onSend(text)
  }

  return (
    <>
      <div className="ze-dialog__backdrop" onClick={onClose} />
      <div className="ze-dialog" data-open="true" data-size="lg" role="dialog" aria-modal="true">
        <header className="ze-dialog__header">
          <span className="ze-dialog__title">{title}</span>
          <button className="ze-button" data-size="sm" data-variant="ghost" type="button" aria-label={t('editor.aiDialogClose')} onClick={onClose}>
            ✕
          </button>
        </header>
        <div className="ze-dialog__body ze-scroll-fade-y" ref={fadeRef}>
          <div className="ze-stack" data-gap="1">
            <label className="ze-field-row" data-density="compact" data-multiline="true">
              <span className="ze-field-row__label">{t('editor.aiDialogIntent')}</span>
              <textarea
                className="ze-textarea"
                data-size="sm"
                data-rows="3"
                value={intent}
                disabled={busy}
                placeholder={placeholder}
                onChange={(event) => setIntent(event.target.value)}
              />
            </label>
            <span className="ze-row" data-gap="1">
              <button className="ze-button" data-size="sm" data-variant="primary" type="button" disabled={busy || !intent.trim()} onClick={send}>
                {t('editor.aiDialogSend')}
              </button>
              {busy && <span className="ze-text" data-role="tertiary">{t('editor.aiDialogThinking')}</span>}
            </span>
            {error && <span className="ze-text" data-tone="danger">{error}</span>}
            {candidate ? (
              <>
                <span className="ze-text" data-role="title">{t('editor.aiDialogPreview')}</span>
                <div className="ze-kv-list" data-size="sm" data-variant="compact">
                  {fields.map((key) => (
                    <div className="ze-kv-list__row" key={key}>
                      <span className="ze-kv-list__key">{fieldLabels?.[key] ?? key}</span>
                      <span className="ze-kv-list__value">{previewValue(candidate[key])}</span>
                    </div>
                  ))}
                </div>
                {note && <span className="ze-text" data-role="tertiary">{note}</span>}
              </>
            ) : (
              !busy && !error && <span className="ze-text" data-role="tertiary">{t('editor.aiDialogEmpty')}</span>
            )}
          </div>
        </div>
        <footer className="ze-dialog__footer">
          <button className="ze-button" data-size="sm" data-variant="ghost" type="button" onClick={onClose}>
            {t('editor.aiDialogCancel')}
          </button>
          <button className="ze-button" data-size="sm" data-variant="primary" type="button" disabled={busy || !candidate} onClick={onAdopt}>
            {t('editor.aiDialogAdopt')}
          </button>
        </footer>
      </div>
    </>
  )
}
