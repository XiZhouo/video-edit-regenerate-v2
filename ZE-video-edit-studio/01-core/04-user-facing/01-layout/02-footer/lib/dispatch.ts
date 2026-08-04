/**
 * Footer dispatch context
 *
 * Footer 所有动作走 command dispatch(SPEC §2.7)。本文件提供 React Context,
 * 子组件用 `useFooterDispatch()` 取 dispatch 函数。
 *
 * 批 2 stub:console.warn(没真 dispatch)
 * 批 4 hook:在 FooterShell 外层用 Provider 注入真实 dispatch(接 01-command)
 */

import { createContext, useContext } from 'react'

import type { FooterCommand, PipelineLifecyclePayload } from '../commands'

export type DispatchFn = (
  command_id: FooterCommand,
  payload: PipelineLifecyclePayload,
) => void

const defaultDispatch: DispatchFn = (command_id, payload) => {
  // eslint-disable-next-line no-console
  console.warn(
    `[ZE Footer] dispatch stub fired: ${command_id}`,
    payload,
    '\n  (no Provider; batch 4 hook will inject real dispatch)',
  )
}

export const FooterDispatchContext = createContext<DispatchFn>(defaultDispatch)

/** 子组件取 dispatch */
export function useFooterDispatch(): DispatchFn {
  return useContext(FooterDispatchContext)
}
