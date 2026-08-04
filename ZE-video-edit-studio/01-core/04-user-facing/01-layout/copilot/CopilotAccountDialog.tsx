/**
 * CopilotAccountDialog — 统一「AI 账号」页:一处绑三样,飞书身份下各绑各的。
 *
 *  · Codex / Claude:订阅账号,傻瓜设备登录(点连接 → 浏览器批准 → 凭据落隔离目录)。
 *  · 组织AI网关:API key,粘一个 key(+可选服务地址)即绑,密封存储。
 *
 * 账号配置只管「绑定+存+取」;哪个账号被哪条管线/功能消费,是消费方各自的事,本页不涉及。
 * copilot 气泡与头部设置菜单进的是同一个本弹窗。视觉全用现有原子 + 结构内联,不新建 CSS。
 */
import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import {
  apiListCredentials, apiSetDefaultCredential, apiDeleteCredential, apiSaveCredential,
  apiCliLoginStart, apiCliLoginPoll, apiCliLoginSubmitCode, apiCliLoginCancel,
  newAuthIdempotencyKey,
  type CredentialRecord, type CliLoginState, type CredentialProvider,
} from '@ze/core/04-user-facing/03-authentication'
import { DiagnosticsUsage } from './DiagnosticsUsage'
import { CapabilitiesDefaults } from './CapabilitiesDefaults'
import type { AiSettingsPage, AiSettingsTarget } from '@ze/core/04-user-facing/03-authentication/aiSettingsLink'

const CLI_PROVIDERS = ['codex_cli_profile', 'claude_cli_profile']
const CURSOR_PROVIDER: CredentialProvider = 'cursor_api_key'
const DEFAULTABLE_PROVIDERS: CredentialProvider[] = [...CLI_PROVIDERS, CURSOR_PROVIDER] as CredentialProvider[]
/** 组织内部 AI 网关的 provider id。**构建期注入,缺省为空** —— 不注入就等于这个渠道不存在。
 *
 *  它此前是写死的字符串常量,于是被打包进每一个 skill 的 dist:对外发布物里明晃晃带着
 *  一个内部渠道的名字(2026-07-26 实测 foundry 的 main-*.js 里就有)。那不是凭据,泄漏的是
 *  「这家公司内部长什么样」。内部构建在 webapp/.env 里设 VITE_ZE_GATEWAY_PROVIDER 打开它;
 *  public 构建什么都不设 —— 字符串根本不进包,不是「进了包但界面藏起来」。 */
const GATEWAY_PROVIDER = String(import.meta.env.VITE_ZE_GATEWAY_PROVIDER ?? '') as CredentialProvider | ''
const GATEWAY_ENABLED = GATEWAY_PROVIDER.length > 0
const SHOWN_PROVIDERS: CredentialProvider[] = GATEWAY_ENABLED
  ? [...DEFAULTABLE_PROVIDERS, GATEWAY_PROVIDER as CredentialProvider]
  : DEFAULTABLE_PROVIDERS

// 复制到剪贴板:安全上下文(HTTPS)用 clipboard API;明文 HTTP(如 amber)无 clipboard API,
// 退回 execCommand('copy')——旧 API 但在非安全上下文也能真复制。返回是否成功。
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard) { await navigator.clipboard.writeText(text); return true }
  } catch { /* 落到 execCommand 兜底 */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch { return false }
}

export type CopilotAccountDialogProps = {
  open: boolean
  onClose: () => void
  onChanged?: () => void
  /** deep-link 落点(见 03-authentication/aiSettingsLink)。缺省 = 连接方式页。 */
  target?: AiSettingsTarget
}

export function CopilotAccountDialog({ open, onClose, onChanged, target }: CopilotAccountDialogProps) {
  const t = useT()
  const [accounts, setAccounts] = useState<CredentialRecord[]>([])
  const [login, setLogin] = useState<CliLoginState | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState(false)
  const [linkCopied, setLinkCopied] = useState(false)
  const urlRef = useRef<HTMLInputElement>(null)
  const [advOpen, setAdvOpen] = useState(false)
  const [advProvider, setAdvProvider] = useState<'codex_cli_profile' | 'claude_cli_profile'>('codex_cli_profile')
  // §3.1 设置中心分页:连接方式(账号绑定,默认)| 能力与默认项 | 诊断与用量。
  const [tab, setTab] = useState<AiSettingsPage>(target?.page ?? 'connect')
  // 弹窗常驻挂载,靠 open 切显隐 —— 落点只在「打开的那一刻」生效,
  // 否则用户手动换了页、外面又派发一次同落点事件,会把人拽回去。
  const openedAt = useRef(false)
  useEffect(() => {
    if (open && !openedAt.current) setTab(target?.page ?? 'connect')
    openedAt.current = open
  }, [open, target?.page])
  const [advFile, setAdvFile] = useState('')
  const advWriteKey = useRef(newAuthIdempotencyKey('credential.upsert.advanced'))
  // 组织AI网关:粘 API key 绑定
  const [gwOpen, setGwOpen] = useState(false)
  const [gwKey, setGwKey] = useState('')
  const [gwUrl, setGwUrl] = useState('')
  const gatewayWriteKey = useRef(newAuthIdempotencyKey('credential.upsert.gateway'))
  // Cursor 服务端多用户:绑定个人 User API Key,不复用宿主机全局 browser login。
  const [cursorOpen, setCursorOpen] = useState(false)
  const [cursorKey, setCursorKey] = useState('')
  const cursorWriteKey = useRef(newAuthIdempotencyKey('credential.upsert.cursor'))

  const reload = useCallback(() => {
    apiListCredentials()
      .then((r) => setAccounts(r.credentials.filter((c) => SHOWN_PROVIDERS.includes(c.provider))))
      .catch((e) => setErr((e as Error).message))
  }, [])

  useEffect(() => {
    if (!open) return
    setErr(''); setLogin(null); setCode(''); setAdvOpen(false)
    setGwOpen(false); setGwKey(''); setGwUrl('')
    setCursorOpen(target?.provider === CURSOR_PROVIDER); setCursorKey('')
    advWriteKey.current = newAuthIdempotencyKey('credential.upsert.advanced')
    gatewayWriteKey.current = newAuthIdempotencyKey('credential.upsert.gateway')
    cursorWriteKey.current = newAuthIdempotencyKey('credential.upsert.cursor')
    reload()
  }, [open, reload, target?.provider])

  const providerName = (p: string): string =>
    p === 'codex_cli_profile' ? t('copilotAccount.codexName')
      : p === 'claude_cli_profile' ? t('copilotAccount.claudeName')
        : p === CURSOR_PROVIDER ? t('copilotAccount.cursorName')
          : t('copilotAccount.gatewayName')

  // Codex:登录发起后,轮询等用户在浏览器批准
  useEffect(() => {
    if (!login || login.status !== 'pending') return
    const id = window.setInterval(async () => {
      try {
        const s = await apiCliLoginPoll(login.login_id)
        if (s.status !== 'pending') {
          setLogin(s)
          if (s.status === 'success') { reload(); onChanged?.() }
        }
      } catch (e) { setErr((e as Error).message) }
    }, 2500)
    return () => window.clearInterval(id)
  }, [login, reload, onChanged])

  const startLogin = async (provider: 'codex' | 'claude') => {
    setErr(''); setBusy(true); setCode(''); setCopied(false); setLinkCopied(false)
    try { setLogin(await apiCliLoginStart(provider)) }
    catch (e) { setErr((e as Error).message) }
    finally { setBusy(false) }
  }
  const submitCode = async () => {
    if (!login || !code.trim() || busy) return
    setBusy(true); setErr('')
    try {
      const s = await apiCliLoginSubmitCode(login.login_id, code.trim())
      setLogin(s)
      if (s.status === 'success') { reload(); onChanged?.() }
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  const cancelLogin = async () => {
    if (login && login.status !== 'success') { try { await apiCliLoginCancel(login.login_id) } catch { /* noop */ } }
    setLogin(null); setCode(''); setErr('')
  }
  const setDefault = async (id: string) => {
    try { await apiSetDefaultCredential(id); reload(); onChanged?.() } catch (e) { setErr((e as Error).message) }
  }
  const remove = async (id: string) => {
    try { await apiDeleteCredential(id); reload(); onChanged?.() } catch (e) { setErr((e as Error).message) }
  }
  const saveAdvanced = async () => {
    if (!advFile.trim() || busy) return
    setBusy(true); setErr('')
    try {
      await apiSaveCredential(
        { provider: advProvider, auth_file: advFile.trim() },
        advWriteKey.current,
      )
      advWriteKey.current = newAuthIdempotencyKey('credential.upsert.advanced')
      setAdvFile(''); setAdvOpen(false); reload(); onChanged?.()
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  const saveGateway = async () => {
    if (!gwKey.trim() || busy) return
    setBusy(true); setErr('')
    try {
      await apiSaveCredential({
        provider: GATEWAY_PROVIDER as CredentialProvider, secret: gwKey.trim(),
        metadata: gwUrl.trim() ? { base_url: gwUrl.trim() } : {},
      }, gatewayWriteKey.current)
      gatewayWriteKey.current = newAuthIdempotencyKey('credential.upsert.gateway')
      setGwKey(''); setGwUrl(''); setGwOpen(false); reload(); onChanged?.()
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  const saveCursor = async () => {
    if (!cursorKey.trim() || busy) return
    setBusy(true); setErr('')
    try {
      await apiSaveCredential(
        { provider: CURSOR_PROVIDER, secret: cursorKey.trim() },
        cursorWriteKey.current,
      )
      cursorWriteKey.current = newAuthIdempotencyKey('credential.upsert.cursor')
      setCursorKey(''); setCursorOpen(false); reload(); onChanged?.()
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  if (!open) return null
  const col: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6 }

  // 打开授权链接:默认浏览器直接开,或复制链接自己粘到想用的(非默认)浏览器。
  const verifyUrl = login?.verify_url ?? ''
  const linkBlock = (
    <>
      <div style={{ display: 'flex', gap: 8 }}>
        <button type="button" className="ze-button" data-variant="primary" style={{ flex: 1 }}
          onClick={() => window.open(verifyUrl, '_blank', 'noopener')}>
          {t('copilotAccount.openLink')}
        </button>
        <button type="button" className="ze-button" style={{ flex: 1 }}
          onClick={async () => {
            if (await copyToClipboard(verifyUrl)) setLinkCopied(true)
            else urlRef.current?.select()   // 极端情况(连 execCommand 都禁)才退回手动选中
          }}>
          {linkCopied ? t('copilotAccount.copied') : t('copilotAccount.copyLink')}
        </button>
      </div>
      <input ref={urlRef} className="ze-input" readOnly value={verifyUrl}
        onFocus={(e) => e.currentTarget.select()} />
      <div className="ze-text" data-role="tertiary">{t('copilotAccount.openLinkHint')}</div>
    </>
  )

  return (
    <div className="ze-dialog__backdrop" role="dialog" aria-label={t('copilotAccount.title')}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="ze-dialog" style={{ width: '100%', maxWidth: 520, maxHeight: '84vh', display: 'flex', flexDirection: 'column' }}>
        <div className="ze-dialog__header"><div className="ze-dialog__title">{t('copilotAccount.title')}</div></div>

        <div className="ze-copilot__acct-tabs" role="tablist" aria-label={t('copilotAccount.title')}>
          <button type="button" role="tab" className="ze-button" data-size="sm"
            data-variant={tab === 'connect' ? 'primary' : 'ghost'} aria-selected={tab === 'connect'}
            onClick={() => setTab('connect')}>{t('copilotAccount.tabConnect')}</button>
          <button type="button" role="tab" className="ze-button" data-size="sm"
            data-variant={tab === 'caps' ? 'primary' : 'ghost'} aria-selected={tab === 'caps'}
            onClick={() => setTab('caps')}>{t('copilotAccount.tabCaps')}</button>
          <button type="button" role="tab" className="ze-button" data-size="sm"
            data-variant={tab === 'diag' ? 'primary' : 'ghost'} aria-selected={tab === 'diag'}
            onClick={() => setTab('diag')}>{t('copilotAccount.diag.title')}</button>
        </div>

        <div className="ze-dialog__body ze-scroll-fade-y" style={{ overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 16 }}>
          {tab === 'diag' ? <DiagnosticsUsage /> : tab === 'caps' ? <CapabilitiesDefaults /> : (
          <>
          {login ? (
            /* ---- 登录中 ---- */
            <section style={col}>
              {login.status === 'success' ? (
                <>
                  <div className="ze-text" data-weight="semibold">{t('copilotAccount.success')}</div>
                  <button type="button" className="ze-button" data-variant="primary" onClick={() => setLogin(null)}>
                    {t('common.gotIt')}
                  </button>
                </>
              ) : login.status === 'error' ? (
                <>
                  <div className="ze-text" data-tone="danger">{login.error || t('copilotAccount.failed')}</div>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button type="button" className="ze-button" onClick={() => setLogin(null)}>{t('copilotAccount.back')}</button>
                  </div>
                </>
              ) : (
                <>
                  <div className="ze-text" data-weight="semibold">
                    {login.provider === 'codex' ? t('copilotAccount.codexStep') : t('copilotAccount.claudeStep')}
                  </div>

                  {login.provider === 'codex' ? (
                    <>
                      {/* Codex:先给验证码(浏览器页面要输入它),再开链接——顺序对上用户操作 */}
                      <div className="ze-text" data-role="secondary">{t('copilotAccount.codexCodeStep')}</div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <kbd className="ze-kbd">{login.user_code}</kbd>
                        <button type="button" className="ze-button" data-variant="primary" data-size="sm"
                          onClick={async () => { if (await copyToClipboard(login.user_code)) setCopied(true) }}>
                          {copied ? t('copilotAccount.copied') : t('copilotAccount.copy')}
                        </button>
                      </div>
                      <div className="ze-text" data-role="secondary">{t('copilotAccount.codexOpenStep')}</div>
                      {linkBlock}
                      <div className="ze-text" data-role="secondary">{t('copilotAccount.waiting')}</div>
                    </>
                  ) : (
                    <>
                      {/* Claude:先开链接登录 → 网页给一串 code → 粘回来 */}
                      <div className="ze-text" data-role="secondary">{t('copilotAccount.step1')}</div>
                      {linkBlock}
                      <div className="ze-text" data-role="secondary">{t('copilotAccount.step2Paste')}</div>
                      <div style={{ display: 'flex', gap: 8 }}>
                        <input className="ze-input" style={{ flex: 1 }} value={code}
                          placeholder={t('copilotAccount.pastePlaceholder')}
                          onChange={(e) => setCode(e.target.value)}
                          onKeyDown={(e) => { if (e.key === 'Enter') void submitCode() }} />
                        <button type="button" className="ze-button" data-variant="primary" disabled={busy || !code.trim()} onClick={() => void submitCode()}>
                          {t('copilotAccount.confirm')}
                        </button>
                      </div>
                    </>
                  )}
                  <button type="button" className="ze-button" data-size="sm" onClick={() => void cancelLogin()}>
                    {t('copilotAccount.cancel')}
                  </button>
                </>
              )}
            </section>
          ) : (
            /* ---- 列表 + 两卡 ---- */
            <>
              <section style={col}>
                {accounts.length === 0 ? (
                  <div className="ze-text" data-role="secondary">{t('copilotAccount.empty')}</div>
                ) : accounts.map((a) => (
                  <div key={a.id} className="ze-kv-list__row">
                    <span className="ze-kv-list__key">
                      {a.label}{a.metadata?.is_default ? ` · ${t('copilotAccount.default')}` : ''}
                    </span>
                    <span className="ze-kv-list__value">
                      {providerName(a.provider)} · {t('copilotAccount.connected')}
                    </span>
                    {DEFAULTABLE_PROVIDERS.includes(a.provider) && !a.metadata?.is_default ? (
                      <button type="button" className="ze-button" data-size="sm" onClick={() => void setDefault(a.id)}>{t('copilotAccount.setDefault')}</button>
                    ) : null}
                    <button type="button" className="ze-button" data-size="sm" data-variant="danger" onClick={() => void remove(a.id)}>{t('copilotAccount.remove')}</button>
                  </div>
                ))}
              </section>

              <div style={{ display: 'flex', gap: 8 }}>
                <button type="button" className="ze-button" data-variant="primary" style={{ flex: 1 }} disabled={busy} onClick={() => void startLogin('codex')}>
                  {t('copilotAccount.connectCodex')}
                </button>
                <button type="button" className="ze-button" data-variant="primary" style={{ flex: 1 }} disabled={busy} onClick={() => void startLogin('claude')}>
                  {t('copilotAccount.connectClaude')}
                </button>
              </div>

              {/* Cursor:服务端自动化只收每个用户自己的 API key,不触碰宿主机全局登录态。 */}
              <div style={col}>
                <button type="button" className="ze-button" data-variant="primary" disabled={busy}
                  onClick={() => setCursorOpen((v) => !v)}>
                  {t('copilotAccount.connectCursor')}
                </button>
                {cursorOpen ? (
                  <div style={col}>
                    <div className="ze-text" data-role="secondary">{t('copilotAccount.cursorStep')}</div>
                    <input className="ze-input" type="password" value={cursorKey}
                      placeholder={t('copilotAccount.cursorKeyPlaceholder')}
                      onChange={(e) => {
                        cursorWriteKey.current = newAuthIdempotencyKey('credential.upsert.cursor')
                        setCursorKey(e.target.value)
                      }} />
                    <div className="ze-text" data-role="tertiary">{t('copilotAccount.cursorHint')}</div>
                    <button type="button" className="ze-button" data-variant="primary"
                      disabled={busy || !cursorKey.trim()} onClick={() => void saveCursor()}>
                      {t('copilotAccount.cursorSave')}
                    </button>
                  </div>
                ) : null}
              </div>

              {/* 组织AI网关:粘 API key(点开内联表单)。未注入 provider id = 本部署没有这个渠道,
                  整块不渲染 —— 与「字符串不进包」是同一个开关,不会出现「按钮在但绑不了」。 */}
              {GATEWAY_ENABLED ? (
              <div style={col}>
                <button type="button" className="ze-button" data-variant="primary" disabled={busy} onClick={() => setGwOpen((v) => !v)}>
                  {t('copilotAccount.connectGateway')}
                </button>
                {gwOpen ? (
                  <div style={col}>
                    <div className="ze-text" data-role="secondary">{t('copilotAccount.gatewayStep')}</div>
                    <input className="ze-input" type="password" value={gwKey}
                      placeholder={t('copilotAccount.gatewayKeyPlaceholder')}
                      onChange={(e) => {
                        gatewayWriteKey.current = newAuthIdempotencyKey('credential.upsert.gateway')
                        setGwKey(e.target.value)
                      }} />
                    <input className="ze-input" value={gwUrl}
                      placeholder={t('copilotAccount.gatewayUrlPlaceholder')}
                      onChange={(e) => {
                        gatewayWriteKey.current = newAuthIdempotencyKey('credential.upsert.gateway')
                        setGwUrl(e.target.value)
                      }} />
                    <div className="ze-text" data-role="tertiary">{t('copilotAccount.gatewayHint')}</div>
                    <button type="button" className="ze-button" data-variant="primary" disabled={busy || !gwKey.trim()} onClick={() => void saveGateway()}>
                      {t('copilotAccount.gatewaySave')}
                    </button>
                  </div>
                ) : null}
              </div>
              ) : null}

              {/* 折叠兜底:手动粘贴凭据 */}
              <div style={col}>
                <button type="button" className="ze-button" data-size="sm" onClick={() => setAdvOpen((v) => !v)}>
                  {t('copilotAccount.advanced')}
                </button>
                {advOpen ? (
                  <div style={col}>
                    <div className="ze-text" data-role="tertiary">{t('copilotAccount.advancedHint')}</div>
                    <div className="ze-select">
                      <select className="ze-select__control" value={advProvider}
                        onChange={(e) => setAdvProvider(e.target.value as 'codex_cli_profile' | 'claude_cli_profile')}>
                        <option value="codex_cli_profile">{t('copilotAccount.codexName')}</option>
                        <option value="claude_cli_profile">{t('copilotAccount.claudeName')}</option>
                      </select>
                    </div>
                    <textarea className="ze-input" rows={4} value={advFile} placeholder={t('copilotAccount.advancedPlaceholder')}
                      style={{ resize: 'vertical' }} onChange={(e) => {
                        advWriteKey.current = newAuthIdempotencyKey('credential.upsert.advanced')
                        setAdvFile(e.target.value)
                      }} />
                    <button type="button" className="ze-button" disabled={busy || !advFile.trim()} onClick={() => void saveAdvanced()}>
                      {t('copilotAccount.advancedSave')}
                    </button>
                  </div>
                ) : null}
              </div>
            </>
          )}

          {err ? <div className="ze-text" data-tone="danger">{err}</div> : null}
          </>
          )}
        </div>

        <div className="ze-dialog__footer" style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button type="button" className="ze-button" onClick={onClose}>{t('common.close')}</button>
        </div>
      </div>
    </div>
  )
}
