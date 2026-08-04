/**
 * dialogRegistry — FormAction `dialog` 字段 → 输入弹窗组件 的注册表(registry-driven 扩展)
 *
 * 带 `dialog:'<key>'` 的动作点击时,ActionBarZone 据本表渲染对应弹窗;弹窗自管参数收集 + dispatch。
 * 新增一种「需输入的命令弹窗」= 加一个组件 + 这里加一行,不改动作渲染管线。首个落地:notify.push。
 */

import type { ComponentType } from 'react'

import type { DispatchFn, EntityDict } from '../types'
import type { FormAction } from './types'
import { PushMessageDialog } from './PushMessageDialog'

export interface DialogProps {
  /** 触发动作(含 command/dialog 等)。 */
  action: FormAction
  /** active 实体(弹窗可据其字段预填收件人/正文)。 */
  entity: EntityDict
  dispatch?: DispatchFn
  onClose: () => void
  /** 可选预填(由调用方据 entity 派生)。 */
  presetRecipient?: string
  presetMessage?: string
}

export const DIALOG_REGISTRY: Record<string, ComponentType<DialogProps>> = {
  'notify.push': PushMessageDialog,
}

export function resolveDialog(key: string | undefined): ComponentType<DialogProps> | undefined {
  return key ? DIALOG_REGISTRY[key] : undefined
}
