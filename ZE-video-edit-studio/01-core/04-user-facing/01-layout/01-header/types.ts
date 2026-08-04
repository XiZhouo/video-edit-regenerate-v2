/**
 * Header 类型定义
 *
 * SPEC: ./SPEC.md §2.6 mod 重载点 + §2.7 跟其他叶子的接口
 */

import { type ReactNode } from 'react'
import type { SyncState } from '../syncState'

// ============================================================
// HeaderConfig — mod 在 02-mod/04-layout.md 声明
// ============================================================

export type HeaderConfig = {
  /** skill 人类可读名,左段显示;未给走 core default "Unnamed Skill" */
  skill_name?: string

  /** skill 图标路径(SVG 单色优先);未给走首字母圆形 fallback */
  skill_icon?: string

  /** 操作手册 md 路径或 url,7.1 帮助菜单走(内联片段取不到时的回退链接) */
  manual?: string

  /** 操作说明语义 HTML 片段路径/url,7.1 优先安全内联;缺省 '/operation-manual.html'。
   * 片段可含安全图片/视频,但 CSS 与可执行内容会由 core sanitizer 移除。 */
  manual_html?: string

  /** 反馈渠道 url,7.4 帮助菜单走;未声明则 7.4 灰显 */
  feedback_url?: string

  /** 最近项目列表长度,5.7 走;默认 10,范围 5..50 */
  recent_max?: number

  /** 快照默认命名格式,5.3 创建快照对话框走;默认 'YYYY-MM-DD HH:mm:ss' */
  snapshot_label_format?: string

  // delete_require_text_confirmation 永远 true(SPEC §2.6 红线),不在 config 里
}

// ============================================================
// HeaderModOverrides — mod 可在 layout 里覆盖默认行为
// ============================================================

export type HeaderModOverrides = {
  // -- 替换 / 覆盖默认值(SPEC §2.6 重载点 1, 5)
  skill_name?: string
  skill_icon?: string

  // -- 替换 slot 内容(SPEC §2.6 重载点 1)
  /** 左段完全自定义(放弃默认 icon + 名) */
  left_slot?: ReactNode
  /** 中段业务自定义(挂在文件菜单右侧) */
  middle_slot?: ReactNode
  /** 右段追加按钮(挂在主题切换前) */
  right_append?: ReactNode

  // -- 追加菜单项(SPEC §2.6 重载点 2)
  /** 文件菜单 9 子项末尾追加 */
  file_menu_append?: ReactNode
  /** 设置菜单 4 子项末尾追加 */
  settings_menu_append?: ReactNode
  /** 帮助菜单 4 子项末尾追加 */
  help_menu_append?: ReactNode

  // -- 隐藏可选子项(SPEC §2.6 重载点 4 白名单)
  hide?: Array<'feedback' | 'rename' | 'delete'>

  // -- 覆盖某个对话框(SPEC §2.6;某 DialogKind 由 mod 自渲染,给 onClose)
  //    例:dialogs={{ new: ({onClose}) => <MyNewDialog onClose={onClose}/> }}
  //    未覆盖的走 core 默认。注:delete 的打字确认红线不受 hide 影响(SPEC §2.6)。
  dialogs?: import('./dialogs').DialogOverrides
}

// ============================================================
// HeaderProps — HeaderShell 入口 props
// ============================================================

export type HeaderProps = {
  /**
   * mod 通过 02-mod/04-layout.md 声明的配置。
   * **完全可选** —— 不传时 core 走全 default 渲染"模板态"
   * (skill_name="Unnamed Skill" / 图标 fallback / 各菜单走默认)
   */
  config?: HeaderConfig

  /** mod 代码层覆盖(slot / 数据扩展 / dialog 替换);完全可选 */
  overrides?: HeaderModOverrides
}

// ============================================================
// HeaderFileItem — 文件切换器列表行(header-v2)
//
// SPEC §2.3.5 文件切换器数据源:App 把 03-authentication 的
// useProject().projects(ProjectSummary)映射注入 HeaderShell.projects。
// ============================================================

export type HeaderFileItem = {
  id: string
  /** Topology generation observed with this list row; required for every row mutation. */
  generation: number
  name: string
  owner?: string
  updated_at: number
  /** 当前用户是否 owner(行内「删除」按钮 gate;服务端另有硬校验) */
  is_owner?: boolean
}

// ============================================================
// SyncState — 来自 02-data-flow/01-realtime-sync;未接入真实源时为 unavailable
//
// SPEC §2.3.4 同步状态指示驱动数据源
// ============================================================

export type { SyncState }

// ============================================================
// CurrentUser — 来自 03-authentication(M1 stub)
//
// SPEC §2.3.9 用户头像数据源
// ============================================================

export type CurrentUser = {
  id: string
  name: string
  email?: string
  avatar_url?: string
  permission?: 'none' | 'read' | 'edit' | 'ban'
  is_super_admin?: boolean
} | null

// ============================================================
// DocumentInfo — 来自 01-document(M1 stub)
//
// SPEC §2.3.3 项目名 + §2.3.4 unsaved 状态数据源
// ============================================================

export type DocumentInfo = {
  id: string
  /** Project topology generation when this document is backed by a project. */
  generation?: number
  name: string
  schema_version: string
  collaboration_mode: 'live_collab' | 'with_save' | 'realtime_checkpoint'
  _v: number
  updated_at: number
  /** 数据目录完整路径(hover 显示) */
  data_dir_path: string
} | null
