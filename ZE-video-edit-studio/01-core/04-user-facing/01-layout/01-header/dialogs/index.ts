/**
 * dialogs — 对外入口
 *
 * 协议: ../SPEC.md §2.3.5-§2.3.7
 *
 * HeaderShell 用:
 *   import { DialogHost, HeaderDialogContext, type DialogKind } from './dialogs'
 * 菜单项用:
 *   import { useHeaderDialog } from './dialogs'
 * mod 覆盖类型:
 *   import { type DialogOverrides } from './dialogs'
 */

export { DialogHost, type DialogHostProps, type DialogOverrides } from './DialogHost'
export {
  HeaderDialogContext, useHeaderDialog,
  type DialogKind, type DialogPayload, type OpenDialogFn,
} from './lib/dialogState'
export { BaseDialog } from './BaseDialog'
