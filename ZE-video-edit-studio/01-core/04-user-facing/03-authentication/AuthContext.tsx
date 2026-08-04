/**
 * AuthContext — 当前登录用户 + token 还原 + 全局 401 踢登录。
 *
 * 挂在 App 最外层(main.tsx)。启动用 localStorage 的 token 调 me() 还原登录态(刷新保持)。
 * 注册 onAuthError 到 authApi:任何 fetch 遇 401 → 清登录态(踢回 LoginPage)。
 */

import {
  createContext, useCallback, useContext, useEffect, useState, type ReactNode,
} from 'react'
import {
  apiLogin, apiLoginWithLark, apiLogout, apiMe, apiRegister, clearToken, setAuthErrorHandler, type AuthUser,
} from './authApi'

type AuthState = {
  user: AuthUser | null
  ready: boolean                                  // 启动 me() 还原是否完成(防闪登录页)
  refreshUser: () => Promise<AuthUser | null>
  loginWithLark: () => Promise<void>
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [ready, setReady] = useState(false)

  const onAuthError = useCallback(() => {
    clearToken()
    setUser(null)
  }, [])

  // 全局 401:任何 fetch(authApi / api.ts)遇 401 → 清登录态。
  useEffect(() => {
    setAuthErrorHandler(onAuthError)
    return () => setAuthErrorHandler(null)
  }, [onAuthError])

  const refreshUser = useCallback(async () => {
    const next = await apiMe()
    setUser(next)
    return next
  }, [])

  // 启动:存量 token → me() 还原(刷新/重开保持登录)。
  useEffect(() => {
    let alive = true
    apiMe()
      .then((u) => { if (alive) setUser(u) })
      .finally(() => { if (alive) setReady(true) })
    return () => { alive = false }
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const r = await apiLogin(username, password)
    setUser(r.user)
  }, [])

  const loginWithLark = useCallback(async () => {
    const r = await apiLoginWithLark()
    setUser(r.user)
  }, [])

  const register = useCallback(async (username: string, password: string) => {
    const r = await apiRegister(username, password)
    setUser(r.user)
  }, [])

  const logout = useCallback(async () => {
    await apiLogout()
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider value={{ user, ready, refreshUser, loginWithLark, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}
