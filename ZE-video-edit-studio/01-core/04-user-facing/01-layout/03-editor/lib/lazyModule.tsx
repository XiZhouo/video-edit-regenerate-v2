/**
 * lazyModule — 通用懒模块注册表(自动下载机制 · 框架级,随 03-editor 一体复制)
 *
 * 这是 core 提供的"机制",不绑定任何具体重库。editor / mod 声明一个重依赖(id + loader),
 * core 负责:去重(同 id 并发只跑一次)、内存缓存(成功后秒回)、loading / error 态、retry。
 *
 * **拉取后端可插拔**:loader 就是后端 —— `() => import('https://esm.sh/three')`(CDN 自动下载)
 * 与 `() => import('three')`(npm 离线)同形,换后端只改这一行,core / 注册表 / editor 不动。
 * core 永不 import 重库:重库的模块值只在运行时由 loader 解析得到。
 *
 * 用法:
 *   registerLazyModule('three-bundle', () => import('https://esm.sh/three@0.169.0'))  // skill 启动时
 *   const m = useLazyModule<ThreeBundle>('three-bundle')  // 组件里:idle/loading/ready/error
 */

import { useCallback, useEffect, useSyncExternalStore } from 'react'

/** loader 即后端:任意 async 函数,resolve 出重模块的值(组件 / 类 / wasm 句柄…)。core 不知道 T 是什么。 */
export type ModuleLoader<T = unknown> = () => Promise<T>

type LazyState<T = unknown> =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; value: T }
  | { status: 'error'; error: Error }

const IDLE: LazyState = { status: 'idle' }

const loaders = new Map<string, ModuleLoader>()
const cache = new Map<string, LazyState>()                 // 引用稳定:同态返回同一对象(useSyncExternalStore 要求)
const inflight = new Map<string, Promise<unknown>>()       // 去重:同 id 并发共用一个 promise
const listeners = new Map<string, Set<() => void>>()

function emit(id: string): void {
  const set = listeners.get(id)
  if (set) for (const l of [...set]) l()
}

function getState(id: string): LazyState {
  return cache.get(id) ?? IDLE
}

function setState(id: string, s: LazyState): void {
  cache.set(id, s)
  emit(id)
}

/** 注册一个懒模块(只记 loader,不预取)。重复注册覆盖。 */
export function registerLazyModule<T>(id: string, loader: ModuleLoader<T>): void {
  loaders.set(id, loader as ModuleLoader)
}

export function unregisterLazyModule(id: string): void {
  loaders.delete(id)
  cache.delete(id)
  inflight.delete(id)
  emit(id)
}

export function isLazyModuleRegistered(id: string): boolean {
  return loaders.has(id)
}

function startLoad(id: string): void {
  const cur = getState(id)
  if (cur.status === 'loading' || cur.status === 'ready') return
  if (inflight.has(id)) return
  const loader = loaders.get(id)
  if (!loader) {
    setState(id, { status: 'error', error: new Error(`lazy module not registered: ${id}`) })
    return
  }
  let promise: Promise<unknown>
  try {
    promise = Promise.resolve(loader())
  } catch (err) {
    setState(id, { status: 'error', error: err instanceof Error ? err : new Error(String(err)) })
    return
  }
  inflight.set(id, promise)
  setState(id, { status: 'loading' })
  promise.then(
    (value) => {
      if (inflight.get(id) === promise) {
        inflight.delete(id)
        setState(id, { status: 'ready', value })
      }
    },
    (err) => {
      if (inflight.get(id) === promise) {
        inflight.delete(id)
        setState(id, { status: 'error', error: err instanceof Error ? err : new Error(String(err)) })
      }
    },
  )
}

/** useLazyModule 的对外返回形态(带 retry/reload 闭包)。 */
export type UseLazyModule<T = unknown> =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; value: T; reload: () => void }
  | { status: 'error'; error: Error; retry: () => void }

/** 订阅一个懒模块;首次使用触发加载,并发去重,成功缓存,暴露 retry。id=null → 恒 idle(opt-out)。 */
export function useLazyModule<T = unknown>(id: string | null): UseLazyModule<T> {
  const subscribe = useCallback(
    (cb: () => void) => {
      if (!id) return () => {}
      let set = listeners.get(id)
      if (!set) {
        set = new Set()
        listeners.set(id, set)
      }
      set.add(cb)
      return () => {
        set!.delete(cb)
      }
    },
    [id],
  )
  const getSnapshot = useCallback((): LazyState => (id ? getState(id) : IDLE), [id])
  const state = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

  useEffect(() => {
    if (id && getState(id).status === 'idle') startLoad(id)
  }, [id])

  const reload = useCallback(() => {
    if (!id) return
    cache.delete(id)
    inflight.delete(id)
    startLoad(id)
  }, [id])

  switch (state.status) {
    case 'ready':
      return { status: 'ready', value: (state as { value: T }).value, reload }
    case 'error':
      return { status: 'error', error: state.error, retry: reload }
    case 'loading':
      return { status: 'loading' }
    default:
      return { status: 'idle' }
  }
}
