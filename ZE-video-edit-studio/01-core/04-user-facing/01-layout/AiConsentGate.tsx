/**
 * AiConsentGate — 首次 AI 生成前的内容合规同意门(对外公测不变量)。
 *
 * 包住声明了 requires_route 的动作入口:未接受当前版本条款时,拦截首次点击(onClickCapture)
 * → 弹条款 + 内容须知(同意 / 取消);同意后 persist(useAiConsent.accept)并放行,用户再点即执行。
 * 已接受(或部署未配条款 / 加载中)时透明透传,零负担。与 AiRouteGate 组合:条款门在外
 * (先接受条款,再谈路线可用),路线门在内。机制在 core,条款文本走 config(ze.ai.terms_*)。
 */
import React, { useState } from 'react'
import { useT } from '../05-preferences'
import { BaseDialog } from './01-header/dialogs/BaseDialog'
import type { AiConsentState } from '../03-authentication/aiConsent'

export function AiConsentGate({ state, children }: {
  state: AiConsentState
  children: React.ReactNode
}): React.ReactNode {
  const t = useT()
  const [open, setOpen] = useState(false)
  // 已接受 / 部署未配条款 / 条款未加载 → 透传(不误伤;加载后若须同意再激活门)
  if (state.accepted) return <>{children}</>
  const terms = state.terms
  const footer = (
    <>
      <button type="button" className="ze-button" data-size="sm" onClick={() => setOpen(false)}>
        {t('common.cancel')}
      </button>
      <button type="button" className="ze-button" data-variant="primary" data-size="sm"
        onClick={() => { state.accept(); setOpen(false) }}>
        {t('aiConsent.accept')}
      </button>
    </>
  )
  return (
    <>
      <span onClickCapture={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(true) }}>
        {children}
      </span>
      {open && (
        <BaseDialog title={t('aiConsent.title')} size="sm" onClose={() => setOpen(false)} footer={footer}>
          <p className="ze-text" data-size="sm">{terms?.text || t('aiConsent.body')}</p>
          <p className="ze-text" data-size="xs" data-ink="tertiary">{t('aiConsent.contentNotice')}</p>
          {terms?.url ? (
            <a className="ze-link" href={terms.url} target="_blank" rel="noreferrer">{t('aiConsent.viewTerms')}</a>
          ) : null}
        </BaseDialog>
      )}
    </>
  )
}
