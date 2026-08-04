type JobLike = {
  data?: {
    status?: unknown
    lastError?: unknown
  }
}

type PipelineStatusLike = {
  phase?: unknown
  stage_label?: unknown
} | null | undefined

export type GenerationJobState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'runtimeMissing'

export function generationJobPresentation(
  job: JobLike,
  pipelineStatus?: PipelineStatusLike,
): { state: GenerationJobState; detail: string; active: boolean } {
  const persisted = String(job.data?.status || 'queued')
  const phase = String(pipelineStatus?.phase || '').toLowerCase()
  const detail = String(job.data?.lastError || '').trim()

  if (persisted === 'requires_runtime') {
    return { state: 'runtimeMissing', detail, active: false }
  }
  if (persisted === 'failed' || persisted === 'cancelled') {
    return { state: 'failed', detail, active: false }
  }
  if (persisted === 'succeeded') {
    return { state: 'succeeded', detail: '', active: false }
  }
  if (phase === 'running' || phase === 'provider_generation') {
    return { state: 'running', detail: '', active: true }
  }
  return { state: 'queued', detail: '', active: true }
}
