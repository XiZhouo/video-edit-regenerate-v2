import { describe, expect, it } from 'vitest'

import { generationJobPresentation } from './generationJobStatus'

describe('generationJobPresentation', () => {
  it('does not present a runtime failure as queued', () => {
    expect(generationJobPresentation({
      data: {
        status: 'requires_runtime',
        lastError: '视频生成服务地址未配置',
      },
    }, { phase: 'done' })).toEqual({
      state: 'runtimeMissing',
      detail: '视频生成服务地址未配置',
      active: false,
    })
  })

  it('keeps a locally accepted task visibly active while it waits or runs', () => {
    expect(generationJobPresentation({
      data: { status: 'queued' },
    }, { phase: 'running' })).toEqual({
      state: 'running',
      detail: '',
      active: true,
    })
  })
})
