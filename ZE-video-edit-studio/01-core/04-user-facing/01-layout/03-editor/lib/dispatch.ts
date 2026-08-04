/**
 * editor dispatch context (镜像 footer/header 的 lib/dispatch 模式)
 *
 * 没有 Provider 注入真实 dispatch 时, 默认是 console.warn 桩 —— 提示接线。
 * webapp 的 provider (useDocument) 注入真实 dispatch: 把 (command_id, args) POST 到
 * 后端 /api/command/{id}, 据响应 / SSE 重新喂 entities props。
 */

import { createContext, useContext } from 'react'
import type { DispatchFn } from '../types'

const defaultDispatch: DispatchFn = (commandId, args) => {
  console.warn(
    `[ZE Editor] dispatch stub fired: ${commandId}`,
    args,
    '\n  (no Provider; webapp useDocument injects the real dispatch)',
  )
}

export const EditorDispatchContext = createContext<DispatchFn>(defaultDispatch)

export function useEditorDispatch(): DispatchFn {
  return useContext(EditorDispatchContext)
}

export type { DispatchFn }
