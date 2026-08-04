/**
 * dialogState — Header dialog 的打开/关闭上下文
 *
 * SPEC: ../../SPEC.md §2.3.5-§2.3.7(各菜单项触发的对话框)
 *
 * 设计:菜单项不再直接 dispatch command,而是 openDialog(kind) 打开对话框;
 * 对话框收集输入后,在确认时才 dispatch(CMD, payload)(经 HeaderDispatchContext)。
 * 纯展示类(about/shortcuts/manual)不 dispatch,只读 config/doc 显示。
 *
 * 跟 lib/dispatch.ts 平级:HeaderShell 同时提供 dispatch 与 dialog 两个 context。
 */

import { createContext, useContext } from 'react'

/** 13 个对话框种类(SPEC 菜单流推导)*/
export type DialogKind =
  | 'new'        // 5.1 新建
  | 'open'       // 5.2 打开(doc 列表)
  | 'snapshot'   // 5.3 创建快照
  | 'save_as'    // 5.4 另存为
  | 'rename'     // 5.8 重命名
  | 'export'     // 5.5 导出(.ze-pack)
  | 'import'     // 5.5b 导入(.ze-pack)
  | 'close'      // 5.6 关闭(条件确认)
  | 'delete'     // 5.9 删除(打字确认红线)
  | 'manual'     // 7.1 操作手册(展示)
  | 'shortcuts'  // 7.2 快捷键(展示)
  | 'about'      // 7.3 关于(展示)
  | 'login'      // 9. 登录(stub)

/** 打开对话框时可带的初始数据(如 rename 预填当前名)*/
export type DialogPayload = Record<string, unknown>

export type OpenDialogFn = (kind: DialogKind, payload?: DialogPayload) => void

const defaultOpen: OpenDialogFn = (kind) => {
  // eslint-disable-next-line no-console
  console.warn(`[ZE Header] openDialog stub: ${kind} (no Provider)`)
}

export const HeaderDialogContext = createContext<OpenDialogFn>(defaultOpen)

/** 子组件(菜单项)取 openDialog */
export function useHeaderDialog(): OpenDialogFn {
  return useContext(HeaderDialogContext)
}
