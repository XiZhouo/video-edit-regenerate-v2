/**
 * taskStages — TaskEditor 的分阶段进度源解析(纯逻辑,无 React)。
 *
 * 两个来源,优先级:
 *   1. SSE 实时运行态 EntityRunStatus.stages —— 多 stage pipeline run 由后端 supervisor
 *      逐 stage emit(P2b);phase/progress 实时。
 *   2. entity.data 静态字段(mod 声明的 stagesField)—— 单 stage / 无 run 时的旧行为。
 *
 * 抽成纯模块便于单测(vitest node 环境,不拉 React)。
 */
import type { EntityDict, EntityRunStatus, StageStatus } from '../types'

export type Stage = { label: string; status?: string; progress?: number }

/** 从 entity.data[field] 读静态 stages(每项 {label/name, status?, progress?})。 */
export function readStages(e: EntityDict, field?: string): Stage[] {
  if (!field) return []
  const raw = e.data[field]
  if (!Array.isArray(raw)) return []
  return raw.map((s, i) => {
    const o = (s && typeof s === 'object' ? s : {}) as Record<string, unknown>
    return {
      label: String(o.label ?? o.name ?? `#${i + 1}`),
      status: typeof o.status === 'string' ? o.status : undefined,
      progress: typeof o.progress === 'number' ? o.progress : undefined,
    }
  })
}

/** 后端 StageStatus → pill 形态(phase→status,null progress→undefined)。 */
function fromLiveStages(stages: StageStatus[]): Stage[] {
  return stages.map((s) => ({
    label: s.label || s.stage_id,
    status: s.phase,
    progress: s.progress ?? undefined,
  }))
}

/** 解析分阶段进度:优先实时 st.stages,缺省退回 entity.data 静态字段。 */
export function resolveStages(
  st: EntityRunStatus | undefined, e: EntityDict, field?: string,
): Stage[] {
  if (st?.stages && st.stages.length > 0) return fromLiveStages(st.stages)
  return readStages(e, field)
}

/** 已知状态(有 i18n,`task.status.*`);其余按原值展示。筛选段控按此固定顺序排已出现的状态。 */
export const KNOWN_STATUS = ['queued', 'running', 'done', 'error', 'idle'] as const
export type KnownStatus = typeof KNOWN_STATUS[number]
export const isKnownStatus = (s: string): s is KnownStatus =>
  (KNOWN_STATUS as readonly string[]).includes(s)

/** 类型库 run_status(07-type-library,持久化结局态)→ 渲染词表别名:
 *  task@1 等库组合实体的 data.status 落在 pending/succeeded/failed/cancelled,
 *  经此映射进 KNOWN_STATUS 的 i18n 与筛选;running 同词免映射(SPEC §10 残差兑现)。
 *  刻意对 live RunPhase 同样生效:phase='pending' 与库词同词同义(等待开始=排队),
 *  映射后从「裸词无标签」变成有 i18n 的 queued——是修复不是越界;RunPhase 其余值
 *  (done/error/skipped/interrupted/stale)不在映射键内,原样透传。 */
export const RUN_STATUS_ALIAS: Record<string, KnownStatus> = {
  pending: 'queued', succeeded: 'done', failed: 'error', cancelled: 'idle',
}

/** 行状态:优先运行态 phase,退回 entity.data.status,统一过别名归一,再退回 'idle'。 */
export function statusOfRow(e: EntityDict, st?: EntityRunStatus): string {
  const raw = st?.phase ?? (e.data.status as string | undefined) ?? 'idle'
  return RUN_STATUS_ALIAS[raw] ?? raw
}

/** 行进度:优先运行态 progress,退回 entity.data.progress。 */
export function rowProgress(e: EntityDict, st?: EntityRunStatus): number | undefined {
  if (st && typeof st.progress === 'number') return st.progress
  const p = e.data.progress
  return typeof p === 'number' ? p : undefined
}
