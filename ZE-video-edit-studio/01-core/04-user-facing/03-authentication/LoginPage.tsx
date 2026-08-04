/**
 * LoginPage — 未登录时的全屏 gate(部署配置驱动)。
 *
 * 不渲染 header/工作台(避免未登录看到文件菜单)。登录方式只能来自服务端公开配置：
 * minimal-local=本地账号，federated=飞书，hybrid=两者并存。
 * 样式走 @css/04-editors/auth.css 的 .ze-login*(只用 --ze-* token,不内联颜色)。
 */

import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useAuth } from './AuthContext'
import {
  apiAuthConfig, type AuthApiError, type AuthMethod, type AuthRuntimeConfig,
} from './authApi'
import { useT } from '@ze/core/04-user-facing/05-preferences'

// layout-region css 随其 React 区域加载(跟 editor/index.tsx 同范式,用 @ze/css alias)
import '@ze/css/05-compositions/access/auth/code.css'

export function LoginPage({ onTryDemo }: { onTryDemo?: () => void } = {}) {
  const { loginWithLark, login, register } = useAuth()
  const t = useT()
  const [config, setConfig] = useState<AuthRuntimeConfig | null>(null)
  const [method, setMethod] = useState<AuthMethod | null>(null)
  const [localScreen, setLocalScreen] = useState<'login' | 'register'>('login')
  const [loginName, setLoginName] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadingConfig, setLoadingConfig] = useState(true)

  const loadConfig = useCallback(async () => {
    setLoadingConfig(true)
    setError('')
    try {
      const next = await apiAuthConfig()
      setConfig(next)
      setMethod(next.preferred_method)
      if (next.registration !== 'open') setLocalScreen('login')
    } catch {
      setConfig(null)
      setMethod(null)
      setError(t('auth.configLoadFailed'))
    } finally {
      setLoadingConfig(false)
    }
  }, [t])

  useEffect(() => {
    void loadConfig()
  }, [loadConfig])

  const submitFeishu = async () => {
    setError('')
    setBusy(true)
    try {
      await loginWithLark()
    } catch (err) {
      const e = err as { message?: string }
      setError(e.message || t('auth.failed'))
    } finally {
      setBusy(false)
    }
  }

  const submitLocal = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (localScreen === 'register') await register(loginName, password)
      else await login(loginName, password)
    } catch (err) {
      const e = err as AuthApiError
      setError(e.code === 'auth_failed' ? t('auth.badCredentials') : (e.message || t('auth.failed')))
    } finally {
      setBusy(false)
    }
  }

  const chooseMethod = (next: AuthMethod) => {
    setMethod(next)
    setError('')
  }

  const demoButton = onTryDemo && (
    <button className="ze-button" data-variant="ghost" type="button" onClick={onTryDemo}>
      {t('auth.tryDemo')}
    </button>
  )

  if (!config || !method) {
    return (
      <div className="ze-login" data-ze-region="login">
        <section className="ze-card ze-login__card" aria-labelledby="ze-login-title">
          <div className="ze-login__title" id="ze-login-title">{t('auth.login.title')}</div>
          <div className="ze-login__hint">
            {loadingConfig ? t('auth.configLoading') : t('auth.configLoadFailed')}
          </div>
          {error && <div className="ze-login__error">{error}</div>}
          {!loadingConfig && (
            <button className="ze-button" data-variant="primary" type="button" onClick={loadConfig}>
              {t('auth.retry')}
            </button>
          )}
          {demoButton}
        </section>
      </div>
    )
  }

  const hasLocal = config.methods.includes('local')
  const hasFeishu = config.methods.includes('feishu')

  if (method === 'local') {
    const registering = localScreen === 'register'
    const emailLogin = config.login_name_format === 'email'
    return (
      <div className="ze-login" data-ze-region="login">
        <section className="ze-card ze-login__card" aria-labelledby="ze-login-title">
          <div className="ze-login__title" id="ze-login-title">
            {registering ? t('auth.register.title') : t('auth.localTitle')}
          </div>
          <div className="ze-login__hint">
            {emailLogin ? t('auth.localEmailBody') : t('auth.localBody')}
          </div>
          {error && <div className="ze-login__error">{error}</div>}
          <form className="ze-stack" data-gap="3" data-align="stretch" onSubmit={submitLocal}>
            <div className="ze-field-row">
              <label className="ze-field-row__label" htmlFor="ze-login-name">
                {emailLogin ? t('auth.emailLoginName') : t('auth.username')}
              </label>
              <input
                className="ze-input"
                id="ze-login-name"
                type={emailLogin ? 'email' : 'text'}
                autoComplete="username"
                value={loginName}
                onChange={(event) => setLoginName(event.target.value)}
                placeholder={emailLogin ? t('auth.emailLoginNameHint') : t('auth.usernameHint')}
                maxLength={64}
                required
                disabled={busy}
              />
            </div>
            <div className="ze-field-row">
              <label className="ze-field-row__label" htmlFor="ze-login-password">
                {t('auth.password')}
              </label>
              <input
                className="ze-input"
                id="ze-login-password"
                type="password"
                autoComplete={registering ? 'new-password' : 'current-password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder={t('auth.passwordHint')}
                minLength={8}
                maxLength={128}
                required
                disabled={busy}
              />
            </div>
            <button className="ze-button" data-variant="primary" type="submit" disabled={busy}>
              {busy ? t('auth.busy') : (registering ? t('auth.submitRegister') : t('auth.submitLogin'))}
            </button>
          </form>
          {config.registration === 'open' ? (
            <button
              className="ze-login__switch"
              type="button"
              disabled={busy}
              onClick={() => {
                setLocalScreen(registering ? 'login' : 'register')
                setError('')
              }}
            >
              {registering ? t('auth.toLogin') : t('auth.toRegister')}
            </button>
          ) : (
            <div className="ze-login__secondary">{t('auth.registrationClosed')}</div>
          )}
          {hasFeishu && (
            <button className="ze-login__switch" type="button" disabled={busy} onClick={() => chooseMethod('feishu')}>
              {t('auth.useFeishu')}
            </button>
          )}
          {demoButton}
        </section>
      </div>
    )
  }

  return (
    <div className="ze-login" data-ze-region="login">
      <section className="ze-card ze-login__card" aria-labelledby="ze-login-title">
        <div className="ze-login__title" id="ze-login-title">{t('auth.feishuTitle')}</div>
        <div className="ze-login__hint">{t('auth.feishuBody')}</div>
        {error && <div className="ze-login__error">{error}</div>}
        <button className="ze-button" data-variant="primary" type="button" disabled={busy} onClick={submitFeishu}>
          {busy ? t('auth.waitingFeishu') : t('auth.submitFeishu')}
        </button>
        {hasLocal && (
          <button className="ze-login__switch" type="button" disabled={busy} onClick={() => chooseMethod('local')}>
            {t('auth.useLocal')}
          </button>
        )}
        {demoButton}
        <div className="ze-login__secondary">{t('auth.feishuHint')}</div>
      </section>
    </div>
  )
}
