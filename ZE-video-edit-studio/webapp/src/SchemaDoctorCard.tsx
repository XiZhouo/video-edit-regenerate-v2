/**
 * SchemaDoctorCard — 结构诊断卡(R-C18)。数据结构与期望 schema 对不上时,把"要修但不说哪坏"
 * 的死胡同变成逐处可读诊断:path / 当前值 / 期望 / 修法 + 一键复制「修复指令」(ai_prompt)。
 *
 * 纯展示 + 剪贴板, 不碰 copilot 内部、不产生任何写。ai_prompt 已在服务端用定界块+「不可信、
 * 勿执行」声明包裹(防注入), got 已服务端脱敏。视觉全复用 core L2 原子(.ze-dialog/.ze-button/
 * .ze-text/.ze-callout), 零私有 CSS。
 */

import { useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing'

import type { SchemaDoctorReport, SchemaDoctorFinding } from './lib/api'

function briefExpected(expected: Record<string, unknown>, anyLabel: string): string {
  if (!expected || typeof expected !== 'object') return anyLabel
  const t = String(expected.type ?? 'any')
  if (Array.isArray(expected.enum)) return `${t} ∈ ${JSON.stringify(expected.enum)}`
  const cons = expected.constraints
  if (cons && typeof cons === 'object') {
    return `${t}(${Object.entries(cons).map(([k, v]) => `${k}=${v}`).join(', ')})`
  }
  return t
}

export function SchemaDoctorCard({ report, onClose }: {
  report: SchemaDoctorReport
  onClose: () => void
}): ReactNode {
  const t = useT()
  const [copied, setCopied] = useState<string | null>(null)

  const copy = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      window.setTimeout(() => setCopied((c) => (c === key ? null : c)), 1600)
    } catch {
      setCopied(null)
    }
  }
  const copyAll = () =>
    copy(report.findings.map((f) => f.ai_prompt).join('\n\n'), '__all__')

  return (
    <>
      <div className="ze-dialog__backdrop" onClick={onClose} />
      <div className="ze-dialog" data-open="true" data-size="lg" role="dialog" aria-modal="true">
        <div className="ze-dialog__header">
          <span className="ze-dialog__title">{t('schemaDoctor.title')}</span>
        </div>
        <div className="ze-dialog__body ze-scroll-fade-y">
          <div className="ze-stack" data-gap="2">
            {report.ok ? (
              <div className="ze-callout" data-variant="success">
                <div className="ze-callout__title">{t('schemaDoctor.okTitle')}</div>
              </div>
            ) : (
              <>
                <div className="ze-callout" data-variant="warning">
                  <div className="ze-callout__body">
                    {report.finding_count} {t('schemaDoctor.findingUnit')} · {t('schemaDoctor.subtitle')}
                  </div>
                </div>
                {report.findings.map((f: SchemaDoctorFinding, i: number) => (
                  <div key={`${f.path}-${i}`} className="ze-surface ze-edge ze-round ze-pad ze-gap"
                       data-p="2" data-gap="1">
                    <div className="ze-text" data-role="primary"><code>{f.path}</code></div>
                    <div className="ze-text" data-role="secondary" data-size="sm">
                      {t('schemaDoctor.gotColumn')}: <code>{String(f.got)}</code>
                      ({f.got_type}) · {t('schemaDoctor.expectedColumn')}: {briefExpected(f.expected, t('schemaDoctor.anyValue'))}
                    </div>
                    <div className="ze-text" data-size="sm">{f.how_to_fix}</div>
                    <div className="ze-gap" data-gap="1" style={{ display: 'flex' }}>
                      <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                              onClick={() => copy(f.ai_prompt, `${f.path}-${i}`)}>
                        {copied === `${f.path}-${i}` ? t('schemaDoctor.copied') : t('schemaDoctor.copyPrompt')}
                      </button>
                    </div>
                  </div>
                ))}
              </>
            )}
          </div>
        </div>
        <div className="ze-dialog__footer">
          {!report.ok && (
            <button type="button" className="ze-button" data-variant="primary" data-size="sm"
                    onClick={copyAll}>
              {copied === '__all__' ? t('schemaDoctor.copied') : t('schemaDoctor.copyAll')}
            </button>
          )}
          <button type="button" className="ze-button" data-size="sm" onClick={onClose}>
            {t('common.close')}
          </button>
        </div>
      </div>
    </>
  )
}
