/**
 * Header 触发的 command_id 字符串常量
 *
 * SPEC: ./SPEC.md §2.7 跟其他叶子的接口
 *
 * Header 不直接 mutate 任何数据 —— 所有动作 dispatch command,经
 * 01-command 通道执行。本文件枚举 Header 允许触发的全部 command_id,
 * 用于:
 *   1. 各菜单 / 控件 onClick 时 dispatch
 *   2. 全局键盘 shortcut 拦截 → dispatch 对应 command
 *   3. audit gate(GATE-UI-HEADER-*)校验 Header 不会 dispatch
 *      不在 HEADER_COMMANDS 列表中的 command
 */

// ============================================================
// 文件菜单 9 子项(SPEC §2.3.5)
// ============================================================

/** 5.1 新建 */
export const CMD_DOCUMENT_NEW = 'document.new'
/** 5.2 打开 */
export const CMD_DOCUMENT_OPEN = 'document.open'
/** 5.3 创建快照(Ctrl/Cmd+S)— 走 04-history-rollback */
export const CMD_DOCUMENT_SNAPSHOT = 'document.snapshot'
/** 5.4 另存为 */
export const CMD_DOCUMENT_SAVE_AS = 'document.save_as'
/** 5.5 导出(.ze-pack 信封;由 sdk.register_manifest_commands 注册,07-type-library/envelope) */
export const CMD_DOCUMENT_EXPORT = 'manifest.export'
/** 5.5b 导入(.ze-pack;App 在 dispatch 缝拦截 payload.file 先上传再转 args.asset) */
export const CMD_MANIFEST_IMPORT = 'manifest.import'
/** 5.6 关闭 */
export const CMD_DOCUMENT_CLOSE = 'document.close'
/** 5.7 最近项目 — UI 内部转 5.2 document.open(已选 doc_id),不单独 command */
/** 5.8 重命名 */
export const CMD_DOCUMENT_RENAME = 'document.rename'
/** 5.10 复制(header-v2;App 拦截转 POST /api/projects/{pid}/duplicate) */
export const CMD_DOCUMENT_DUPLICATE = 'document.duplicate'
/** 5.9 删除项目 */
export const CMD_DOCUMENT_DELETE = 'document.delete'
/** 5.10 共享 ACL(owner-only;ShareButton 加/移除协作者)*/
export const CMD_DOCUMENT_SET_ACL = 'document.set_acl'
/** 示例:打开 / 重置一个 demo 沙盒(App 拦截转 ProjectContext.openDemoTemplate;
 *  与 document.open 同型 —— 工作台层项目切换,非后端 Operation)*/
export const CMD_DEMO_OPEN = 'demo.open'

// ============================================================
// 设置菜单(SPEC §2.3.6)
// ============================================================
// 6.1 主题 / 6.2 语言:**偏好,不走 command/dispatch** —— 由 PreferencesProvider
//   (04-user-facing/05-preferences)直接管(localStorage + <html data-theme/lang>);
//   偏好属 Rendering Hint 层,非 domain data、不可 undo,故不进 Operation/command 通道。
// 6.3 数据目录 — 只读复制 / 6.4 协作状态 — 只读;均不触发 command。

// ============================================================
// 帮助菜单 4 子项(SPEC §2.3.7)
// ============================================================

/** 7.1 操作手册 */
export const CMD_SYSTEM_HELP_MANUAL = 'system.help.manual'
/** 7.2 快捷键面板 */
export const CMD_SYSTEM_HELP_SHORTCUTS = 'system.help.shortcuts'
/** 7.3 版本 */
export const CMD_SYSTEM_HELP_ABOUT = 'system.help.about'
/** 7.4 反馈 */
export const CMD_SYSTEM_HELP_FEEDBACK = 'system.help.feedback'

// ============================================================
// 用户头像(SPEC §2.3.9)
// ============================================================

/** 登录 */
export const CMD_AUTH_LOGIN = 'auth.login'
/** 登出 */
export const CMD_AUTH_LOGOUT = 'auth.logout'
/** 切换账号 */
export const CMD_AUTH_SWITCH = 'auth.switch'

// ============================================================
// 全集 — Header 允许 dispatch 的 command 集合
//
// audit gate 校验:Header 只能 dispatch 此列表内 command;dispatch
// 任何业务 mutation command(改 entity / 改 doc 字段以外) 一律拒绝
// ============================================================

export const HEADER_COMMANDS = [
  CMD_DOCUMENT_NEW,
  CMD_DOCUMENT_OPEN,
  CMD_DOCUMENT_SNAPSHOT,
  CMD_DOCUMENT_SAVE_AS,
  CMD_DOCUMENT_EXPORT,
  CMD_MANIFEST_IMPORT,
  CMD_DOCUMENT_CLOSE,
  CMD_DOCUMENT_RENAME,
  CMD_DOCUMENT_DUPLICATE,
  CMD_DOCUMENT_DELETE,
  CMD_DOCUMENT_SET_ACL,
  CMD_DEMO_OPEN,
  CMD_SYSTEM_HELP_MANUAL,
  CMD_SYSTEM_HELP_SHORTCUTS,
  CMD_SYSTEM_HELP_ABOUT,
  CMD_SYSTEM_HELP_FEEDBACK,
  CMD_AUTH_LOGIN,
  CMD_AUTH_LOGOUT,
  CMD_AUTH_SWITCH,
] as const

export type HeaderCommand = (typeof HEADER_COMMANDS)[number]

// ============================================================
// 键盘 shortcut 映射(批 2 的 useKeybindings hook 用)
//
// 命中后:event.preventDefault() + dispatch 对应 command
// ============================================================

export type ShortcutMap = Record<string, HeaderCommand>

export const HEADER_SHORTCUTS: ShortcutMap = {
  // 文件
  'mod+n': CMD_DOCUMENT_NEW,
  'mod+o': CMD_DOCUMENT_OPEN,
  'mod+s': CMD_DOCUMENT_SNAPSHOT,         // 关键:Ctrl+S 走快照(SPEC §2.3.5.3)
  'mod+shift+s': CMD_DOCUMENT_SAVE_AS,
  'mod+w': CMD_DOCUMENT_CLOSE,             // 需 preventDefault 否则浏览器关 tab

  // 帮助
  'f1': CMD_SYSTEM_HELP_MANUAL,
  '?': CMD_SYSTEM_HELP_SHORTCUTS,          // 单键无修饰
}

// `mod` 在 Mac = Cmd / 在 Win / Linux = Ctrl;批 2 useKeybindings 实现映射
