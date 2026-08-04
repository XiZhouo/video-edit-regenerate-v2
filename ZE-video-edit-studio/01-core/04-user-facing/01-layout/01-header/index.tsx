/**
 * Header — 对外入口
 *
 * 协议: ./SPEC.md
 * 视觉: 03-css-rules/04-editors/header.css(在 HeaderShell.tsx 顶部 import)
 *
 * layout 父级(01-layout/SPEC.md §2.1)装载 Header 时:
 *   import { Header } from './01-header'
 *
 * mod 想自定义子组件 / 用法时,可单独引各 Segment / 类型:
 *   import { LeftSegment, type HeaderModOverrides } from './01-header'
 */

// 同时 export 两个名字:
//   - HeaderShell:跟实现文件同名,清楚
//   - Header:简洁别名,App.tsx 推荐用
export { HeaderShell, HeaderShell as Header } from './HeaderShell'

export { LeftSegment } from './LeftSegment'
export { MiddleSegment } from './MiddleSegment'
export { RightSegment } from './RightSegment'

// 对话框系统(批 3):宿主 + context + mod 覆盖类型
export {
  DialogHost, HeaderDialogContext, useHeaderDialog, BaseDialog,
  type DialogKind, type DialogPayload, type DialogOverrides,
} from './dialogs'

export type {
  HeaderProps,
  HeaderConfig,
  HeaderModOverrides,
  HeaderFileItem,
  SyncState,
  CurrentUser,
} from './types'

export {
  HEADER_COMMANDS,
  CMD_DOCUMENT_NEW,
  CMD_DOCUMENT_OPEN,
  CMD_DOCUMENT_SNAPSHOT,
  CMD_DOCUMENT_SAVE_AS,
  CMD_DOCUMENT_RENAME,
  CMD_DOCUMENT_DUPLICATE,
  CMD_DOCUMENT_EXPORT,
  CMD_DOCUMENT_CLOSE,
  CMD_DOCUMENT_DELETE,
  CMD_SYSTEM_HELP_MANUAL,
  CMD_SYSTEM_HELP_SHORTCUTS,
  CMD_SYSTEM_HELP_ABOUT,
  CMD_SYSTEM_HELP_FEEDBACK,
  CMD_AUTH_LOGIN,
  CMD_AUTH_LOGOUT,
  CMD_AUTH_SWITCH,
  type HeaderCommand,
} from './commands'
