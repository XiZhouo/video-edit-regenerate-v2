/**
 * SyncState — 02-data-flow/01-realtime-sync 的前端只读投影。
 *
 * 语义:
 *   - unavailable: 当前 surface 没有接入真实同步源。
 *   - syncing: 本地变更已发出,仍等待 HTTP / SSE 确认。
 *   - delayed: 服务端已确认,但事件流尚未追上,或 pending 超过延迟阈值。
 *   - in-sync: 本地 pending 已清空,事件流健康。
 *   - conflict: 后端拒绝写入(如 409/version conflict),需要用户处理。
 *   - offline: 文档 API 或事件流离线。
 */

export type SyncState =
  | { kind: 'unavailable' }
  | { kind: 'in-sync' }
  | { kind: 'syncing'; pending_op_count: number }
  | { kind: 'delayed'; delay_ms: number; pending_op_count?: number }
  | { kind: 'conflict'; conflict_count: number; trace_ids: string[] }
  | { kind: 'offline' }

export const SYNC_DELAY_MS = 2_000
