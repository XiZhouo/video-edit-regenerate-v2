/**
 * Header dispatch context
 *
 * Header 所有动作走 command dispatch(SPEC §2.7)。本文件提供 React Context,
 * 子组件用 `useHeaderDispatch()` 取 dispatch 函数。
 *
 * 批 2 stub:console.warn(没真 dispatch)
 * 批 4 hook:在 HeaderShell 外层用 Provider 注入真实 dispatch(接 01-command 通道)
 */

import { createContext, useContext } from 'react'

import type { HeaderCommand } from '../commands'

export type DispatchPayload = Record<string, unknown>

export type DispatchFn = (
  command_id: HeaderCommand,
  payload?: DispatchPayload,
) => void

const defaultDispatch: DispatchFn = (command_id, payload) => {
  // eslint-disable-next-line no-console
  console.warn(
    `[ZE Header] dispatch stub fired: ${command_id}`,
    payload ?? {},
    '\n  (no Provider; batch 4 hook will inject real dispatch)',
  )
}

export const HeaderDispatchContext = createContext<DispatchFn>(defaultDispatch)

/** 子组件取 dispatch */
export function useHeaderDispatch(): DispatchFn {
  return useContext(HeaderDispatchContext)
}
