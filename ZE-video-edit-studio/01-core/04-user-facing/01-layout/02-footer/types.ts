/**
 * Footer 类型定义
 *
 * SPEC: ./SPEC.md §2.2-§2.4 + §2.6 mod 重载点
 *
 * 设计原则:
 *   - 所有视觉状态由 CSS data-* 驱动(footer.css 是唯一改皮入口)
 *   - 类型只定数据形态;不暗含 React 实现细节
 */

import { type ReactNode } from 'react'
import type { PersistenceState } from '../persistenceState'
import type { SyncState } from '../syncState'

// ============================================================
// 状态类型(对应 footer.css 里 data-state="*")
// ============================================================

/** 服务器连通状态(SPEC §2.2 第一 segment)*/
export type ServerStatus = 'online' | 'offline' | 'connecting'

/** 同步状态(SPEC §2.2 同步 segment + §2.3 同步详情)*/
export type { SyncState }
/** 持久化状态与 SyncState 独立；前者回答“是否保存”，后者回答“协作流是否追上”。 */
export type { PersistenceState }

/** Op log 单条(SPEC §2.3 最近 30 条)*/
export type OpLogEntry = {
  id: string
  trace_id: string
  command_id: string
  /** 人类可读的 command 名(已 i18n)*/
  command_label: string
  /** 触发源 — 决定折叠色标 */
  source: 'user' | 'ai' | 'sync' | 'system'
  status: 'success' | 'failure' | 'cancelled'
  /** 完整耗时;ms */
  duration_ms: number
  /** epoch ms */
  timestamp: number
  /** 失败时摘要(完整 stack 在 dialog)*/
  error_message?: string
}

/** Pipeline 结构化执行日志(来自 pipeline_status 的加性字段)。 */
export type PipelineLogEvent = {
  ts: number
  level: 'info' | 'warn' | 'error' | string
  source: 'pipeline' | 'stage' | 'progress' | string
  run_id: string
  trace_id: string
  message: string
  item_id?: string
  stage_id?: string
  code?: string
  artifact_path?: string
  progress?: number
}

/** Pipeline 当前非 done 项摘要。 */
export type PipelineActiveItem = {
  item_id: string
  entity_type: string
  entity_id: string
  phase: string
  progress?: number | null
  stage_label?: string
  stage_index?: number | null
  error?: { code?: string; message?: string; trace_id?: string } | null
}

/** Pipeline 产物引用摘要。 */
export type PipelineArtifact = {
  item_id: string
  path: string
  present: boolean
}

export type PipelinePort = {
  id: string
  label?: string
  kind:
    | 'text' | 'number' | 'boolean' | 'json' | 'entity' | 'asset' | 'image'
    | 'mask' | 'bbox' | 'table' | 'metric' | 'patch' | 'log' | 'artifact'
    | string
  source:
    | 'args' | 'work_item_payload' | 'entity_field' | 'config' | 'stage_output'
    | 'artifact' | 'operation_patch' | string
  field?: string
  path?: string
  entityType?: string
  preview?: 'inline' | 'thumbnail' | 'full' | 'json' | 'kv' | 'diff' | 'none' | string
  required?: boolean
  secret?: boolean
}

export type PipelineStepState = {
  item_id: string
  entity_type: string
  entity_id: string
  stage_id: string
  stage_label?: string
  stage_index?: number
  command_id: string
  phase: 'pending' | 'running' | 'done' | 'error' | 'skipped' | 'interrupted' | 'stale' | string
  progress?: number | null
  started_at?: number
  ended_at?: number
  input_snapshot?: Record<string, unknown>
  output_snapshot?: Record<string, unknown>
  artifact_refs?: PipelineArtifact[]
  operation_ids?: string[]
  error?: { code?: string; message?: string; trace_id?: string } | null
}

export type CommandSpecBrief = {
  id: string
  label: string
  kind?: string
  description?: string
  params_schema?: Record<string, unknown>
  outputs_schema?: Record<string, unknown>
  ui_ports?: { inputs?: PipelinePort[]; outputs?: PipelinePort[]; [k: string]: unknown }
  /** AI 通道声明('image'/'llm'/…);声明后前端入口据 aiRoutes 做按钮门控。可选、加性。 */
  requires_route?: string
}

export type PipelineStaticMeta = {
  id: string
  label?: string
  stages?: Array<{
    id: string
    label?: string
    command_id?: string
    input_ports?: PipelinePort[]
    output_ports?: PipelinePort[]
  }>
}

/** Pipeline 运行实例(SPEC §2.3 pipeline 列表)*/
export type PipelineRun = {
  id: string
  pipeline_id?: string
  start_command_id?: string
  /** 人类可读名(skill 自定义)*/
  name: string
  /** 当前阶段 label */
  stage: string
  /** 0..1;undefined 表示 indeterminate */
  progress?: number
  status: 'running' | 'paused' | 'interrupting' | 'interrupted' | 'cancelling' | 'cancelled' | 'success' | 'error'
  started_at: number
  ended_at?: number | null
  /** 折叠日志;最近 N 行;UI 默认折叠,点开看 */
  log_lines: string[]
  /** lifecycle 控制权限(SPEC §2.6 mod 可追加按钮,核心 3 个不可删)*/
  can_pause?: boolean
  can_interrupt?: boolean
  can_resume?: boolean
  can_cancel?: boolean
  /** 失败 item 数(>0 时显示"全部重试失败";来自 supervisor 文件系统派生)*/
  failed_count?: number
  /** 剩余预估秒(P2d;线性外推,样本不足时缺省)*/
  eta_sec?: number
  /** 吞吐 item/秒(P2d;缺省 = 样本不足)*/
  throughput?: number
  /** 多 stage run 的每 stage 完成统计(P2d;单 stage 缺省)*/
  stages?: { stage_id: string; label: string; done: number; total: number }[]
  /** 结构化详情协议版本;缺省表示旧 payload。 */
  detail_version?: 1
  /** 结构化执行日志;缺省时 UI 回退 log_lines。 */
  log_events?: PipelineLogEvent[]
  /** 当前非 done 项,用于展开详情。 */
  active_items?: PipelineActiveItem[]
  /** 声明产物的 bounded 摘要。 */
  artifacts?: PipelineArtifact[]
  /** Notebook editor 的 step 级运行态和快照。 */
  step_states?: PipelineStepState[]
  /** M1 运行期已「关闭」(halt-after)断点的 stage_id 集;pipeline.set_breakpoint 切。
   *  末 stage 设了也 no-op、单 stage run 不消费——前端只在多 stage 非末步显 toggle。 */
  breakpoints?: string[]
  /** M1 stage 总数;>1 才显每步断点 toggle、末步(index===count-1)不显(halt-after 末步无意义)。 */
  stage_count?: number
}

/** 错误事件(SPEC §2.3 错误列表 tab)*/
export type ErrorEntry = {
  id: string
  trace_id: string
  level: 'error' | 'warn'
  /** 简短可读消息(点击进 dialog 看完整 stack)*/
  message: string
  /** epoch ms */
  timestamp: number
}

/** trace 链(SPEC §2.3 trace-id 链 tab)*/
export type TraceLink = {
  trace_id: string
  layers: Array<{
    /** UI / API / command / operation / oplog 之一 */
    layer: string
    /** epoch ms */
    timestamp: number
    duration_ms?: number
    /** 简短描述(URL / command id / op id 等)*/
    detail?: string
  }>
}


// ============================================================
// FooterConfig — mod 在 02-mod/04-layout.md 声明
// ============================================================

export type FooterConfig = {
  /** 折叠态显示 skill 名;未给走 'Unnamed Skill' */
  skill_name?: string

  /** Op log 显示条数上限,默认 30(对齐 SPEC §2.3)*/
  oplog_max?: number

  /** 展开态默认高度 px,默认 240(对齐 SPEC §2.5)*/
  expanded_height_default?: number

  /** 用户拖到最小高度 px,默认 24(= 折叠);<24 视为 toggle 到折叠 */
  expanded_height_min?: number

  /** success pipeline 自动移除延迟 ms;0 = 不自动移除,默认 3000 */
  pipeline_success_autoremove_ms?: number
}

// ============================================================
// FooterModOverrides — mod 代码层重载
//
// 红线:不可让 mod 注入"业务数据 mutation"按钮(SPEC §2.6)
// 这里只允许 pipeline lifecycle 范畴 + 折叠态 segment / 展开 tab 追加
// ============================================================

export type FooterModOverrides = {
  /** 折叠态末尾追加 segment(SPEC §2.6 重载点 1)*/
  segment_append?: ReactNode

  /** 完整替换折叠态(SPEC §2.6 重载点 2);谨慎用 */
  collapsed_slot?: ReactNode

  /** 展开"详情"区追加 tab(SPEC §2.6 重载点 3)*/
  details_tabs_append?: Array<{
    id: string
    label: string
    render: () => ReactNode
    count?: number
    variant?: 'default' | 'danger'
  }>

  /** Pipeline 卡片追加 lifecycle 按钮(SPEC §2.6 重载点 4)
   *  返回的 ReactNode 会跟默认 pause/stop/resume 同行;
   *  audit gate 校验返回内容只能 dispatch pipeline.* command
   */
  pipeline_controls_append?: (run: PipelineRun) => ReactNode

  /** 隐藏可选 segment(SPEC §2.6 白名单)*/
  hide?: Array<'progress' | 'errors' | 'trace_id' | 'sync' | 'server' | 'skill_name'>
}


// ============================================================
// FooterProps — FooterShell 入口
//
// **完全可选** —— 不传任何 props,core 走全 default 渲染"模板态"
// (server=online / sync=unavailable / 0 错误 / 空 oplog / 空 pipelines / 空 errors)
// ============================================================

export type FooterProps = {
  config?: FooterConfig
  overrides?: FooterModOverrides

  // ------------------------------------------------------------
  // 数据源(批 4 hook 提供;未给走 stub)
  // ------------------------------------------------------------
  /** 服务器状态;default 'online' */
  server?: ServerStatus
  /** 同步状态;default { kind: 'unavailable' } */
  sync?: SyncState
  /** 持久化状态;default { kind: 'unavailable' } */
  persistence?: PersistenceState
  /** 自上次清零起的累计错误数;default 0 */
  errorCount?: number
  /** 当前 trace-id 短码(6-8 字符);default 无 */
  traceIdShort?: string
  /** 最近 op(已倒序,最新在 [0]);default [] */
  oplog?: OpLogEntry[]
  /** 活跃 pipeline;default [] */
  pipelines?: PipelineRun[]
  /** 错误列表(完整);default [] */
  errors?: ErrorEntry[]
  /** 当前 trace 链(完整);default null */
  traceLink?: TraceLink | null

  // ------------------------------------------------------------
  // 状态控制(受控/非受控)
  // ------------------------------------------------------------
  /** 展开/折叠;受控 */
  expanded?: boolean
  onExpandedChange?: (expanded: boolean) => void
  /** 展开态高度 px;受控 */
  expandedHeight?: number
  onExpandedHeightChange?: (height: number) => void

  // ------------------------------------------------------------
  // 历史回滚(undo/redo;OpLog 面板头部按钮 + 全局 ⌘Z/⌘⇧Z)
  //
  // undo/redo 走后端独立路由(/api/undo·/api/redo),不是 dispatch command,
  // 所以单独传回调 + 可用态;四者全缺省 → OpLog 头部不显示按钮(模板态)。
  // ------------------------------------------------------------
  /** 是否可撤销(后端 can_undo);未给 = false */
  canUndo?: boolean
  /** 是否可重做(后端 can_redo);未给 = false */
  canRedo?: boolean
  /** 撤销触发;未给则不渲染 undo 按钮 */
  onUndo?: () => void
  /** 重做触发;未给则不渲染 redo 按钮 */
  onRedo?: () => void

  // ------------------------------------------------------------
  // dispatch(批 4 hook 接 01-command)
  // ------------------------------------------------------------
  /** 真实 dispatch;未提供则 Context 默认 stub(console.warn)*/
  dispatch?: import('./lib/dispatch').DispatchFn
}
