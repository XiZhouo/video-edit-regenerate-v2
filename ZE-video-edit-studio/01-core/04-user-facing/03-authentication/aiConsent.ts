/**
 * AI 内容合规同意(对外公测门:首次 AI 生成前须接受条款 + 内容须知)。
 *
 * 消费 GET /api/ai-consent:按部署配置(ze.ai.terms_*)返回 {text, url, version}。
 * useAiConsent() 把「已接受版本」存 localStorage(ze.ai.consent.v1);条款 version 变更
 * (律师定稿 / 内容政策更新)自动要求重新同意。terms.version 为空 = 部署未配条款 →
 * accepted 恒 true(门不激活;本地开发与未配 skill 零影响,向后兼容)。
 * 配套 AiConsentGate(01-layout/AiConsentGate.tsx):声明了 requires_route 的动作入口,
 * 未同意时拦首击弹条款,同意后透传。机制在 core,条款文本是 policy 由 config 注入。
 */
import { useCallback, useEffect, useState } from 'react'
import { authHeader, triggerAuthError } from './authApi'

export type AiConsentTerms = { text: string; url: string; version: string }

export type AiConsentState = {
  /** 已接受当前版本条款(或部署未配条款 = 无需同意)。 */
  accepted: boolean
  loaded: boolean
  terms: AiConsentTerms | null
  accept: () => void
}

const STORAGE_KEY = 'ze.ai.consent.v1'

export async function apiGetAiConsent(): Promise<AiConsentTerms> {
  const res = await fetch('/api/ai-consent', { headers: authHeader() })
  if (res.status === 401) { triggerAuthError(); throw new Error('auth') }
  if (!res.ok) throw new Error(`ai-consent ${res.status}`)
  return res.json()
}

function readAccepted(): string {
  try { return window.localStorage.getItem(STORAGE_KEY) ?? '' } catch { return '' }
}

export function useAiConsent(): AiConsentState {
  const [terms, setTerms] = useState<AiConsentTerms | null>(null)
  const [acceptedVersion, setAcceptedVersion] = useState<string>(() => readAccepted())
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    apiGetAiConsent()
      .then((t) => { setTerms(t); setLoaded(true) })
      .catch(() => setLoaded(true))   // 拿不到条款配置 → 不激活门(向后兼容,不误伤)
  }, [])

  const accept = useCallback(() => {
    const v = terms?.version ?? ''
    try { window.localStorage.setItem(STORAGE_KEY, v) } catch { /* 存储不可用:仅本会话内接受 */ }
    setAcceptedVersion(v)
  }, [terms])

  // 部署未配条款(version 空)→ 无需同意;否则须 localStorage 版本 === 当前条款版本。
  const version = terms?.version ?? ''
  const accepted = version === '' || acceptedVersion === version
  return { accepted, loaded, terms, accept }
}
