/**
 * pipelineNotebookExport — 管线 notebook 只读导出(纯函数,无写副作用 → 不需要命令;
 * 与 copy-output / download-output 同类,「所有写走 dispatch」不约束读导出)。
 *
 * JSON:run 元信息 + steps(按 stage_index 排)+ 快照/产物/操作/错误/日志。
 * Markdown:人读复盘文档(每 step 一节)。asset 值导出 sha256 引用与字段名,不内联二进制。
 */

import type { CommandSpecBrief, PipelineLogEvent, PipelineRun, PipelineStepState } from '../../02-footer'

function sortedSteps(run: PipelineRun): PipelineStepState[] {
  return (run.step_states ?? []).slice().sort((a, b) => (a.stage_index ?? 0) - (b.stage_index ?? 0))
}

function stepLogs(run: PipelineRun, s: PipelineStepState): PipelineLogEvent[] {
  return (run.log_events ?? []).filter((l) =>
    (!l.item_id || l.item_id === s.item_id) && (!l.stage_id || l.stage_id === s.stage_id))
}

/** 快照值瘦身:asset 引用(含 sha256)只留 {sha256};其余原样(JSON 可序列化)。 */
function leanSnapshot(snap?: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(snap ?? {})) {
    const rec = v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
    const sha = rec
      ? (typeof rec.sha256 === 'string' ? rec.sha256
        : rec.ref && typeof rec.ref === 'object' && typeof (rec.ref as Record<string, unknown>).sha256 === 'string'
          ? (rec.ref as Record<string, unknown>).sha256 : null)
      : null
    out[k] = sha ? { sha256: sha } : v
  }
  return out
}

export function notebookToJson(run: PipelineRun, commands: Map<string, CommandSpecBrief>): string {
  return JSON.stringify({
    schema: 'ze-pipeline-notebook.v1',
    run: {
      id: run.id, name: run.name ?? '', pipeline_id: run.pipeline_id ?? '',
      status: run.status, stage: run.stage, progress: run.progress ?? null,
      started_at: run.started_at ?? null, failed_count: run.failed_count ?? 0,
    },
    steps: sortedSteps(run).map((s) => ({
      item_id: s.item_id, stage_id: s.stage_id, stage_index: s.stage_index ?? null,
      command_id: s.command_id ?? '', command_label: commands.get(s.command_id ?? '')?.label ?? '',
      phase: s.phase, progress: s.progress ?? null,
      started_at: s.started_at ?? null, ended_at: s.ended_at ?? null,
      input_snapshot: leanSnapshot(s.input_snapshot),
      output_snapshot: leanSnapshot(s.output_snapshot),
      operation_ids: s.operation_ids ?? [],
      error: s.error ?? null,
      logs: stepLogs(run, s).map((l) => ({ ts: l.ts, level: l.level, message: l.message })),
    })),
  }, null, 2)
}

export function notebookToMarkdown(run: PipelineRun, commands: Map<string, CommandSpecBrief>): string {
  const lines: string[] = []
  lines.push(`# ${run.name || run.pipeline_id || run.id}`)
  lines.push('')
  lines.push(`- run: \`${run.id}\``)
  lines.push(`- status: ${run.status} · stage: ${run.stage}`)
  if (typeof run.progress === 'number') lines.push(`- progress: ${Math.round(run.progress * 100)}%`)
  if (run.failed_count) lines.push(`- failed: ${run.failed_count}`)
  lines.push('')
  for (const s of sortedSteps(run)) {
    const label = commands.get(s.command_id ?? '')?.label || s.stage_label || s.command_id || s.stage_id
    lines.push(`## ${(s.stage_index ?? 0) + 1}. ${label} — ${s.phase}`)
    lines.push('')
    lines.push(`- item: \`${s.item_id}\` · stage: \`${s.stage_id}\``)
    if (s.error) lines.push(`- error: **${s.error.code ?? 'error'}** ${s.error.message ?? ''}`)
    const dumpSnap = (title: string, snap?: Record<string, unknown>) => {
      const lean = leanSnapshot(snap)
      if (Object.keys(lean).length === 0) return
      lines.push('')
      lines.push(`### ${title}`)
      lines.push('```json')
      lines.push(JSON.stringify(lean, null, 2))
      lines.push('```')
    }
    dumpSnap('inputs', s.input_snapshot)
    dumpSnap('outputs', s.output_snapshot)
    const logs = stepLogs(run, s)
    if (logs.length) {
      lines.push('')
      lines.push('### logs')
      for (const l of logs) lines.push(`- [${l.level}] ${l.message}`)
    }
    lines.push('')
  }
  return lines.join('\n')
}
