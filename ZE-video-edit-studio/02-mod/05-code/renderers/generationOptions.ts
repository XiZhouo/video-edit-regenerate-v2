export type GenerationProvider = 'omni' | 'seedance'
export type GuidanceStrategy = 'annotated_video' | 'modified_frames'

export type GenerationModelOption = {
  id: string
  label: string
  provider: GenerationProvider
}

/** 产品只暴露三个可直接选择的模型；provider 是提交协议的内部字段。 */
export const GENERATION_MODELS: GenerationModelOption[] = [
  {
    id: 'gemini-omni-flash-preview',
    label: 'Omni',
    provider: 'omni',
  },
  {
    id: 'seedance-2.0',
    label: 'Seedance Pro',
    provider: 'seedance',
  },
  {
    id: 'seedance-2.0-fast',
    label: 'Seedance Fast',
    provider: 'seedance',
  },
]

const DEFAULT_MODEL = GENERATION_MODELS[0]

export function modelOption(modelId: string): GenerationModelOption {
  return GENERATION_MODELS.find((option) => option.id === modelId) ?? DEFAULT_MODEL
}

export function strategiesForModel(_modelId: string): GuidanceStrategy[] {
  // 产品暂不提前判断模型与提交方式的兼容性；服务端实际响应才是最终结果。
  return ['annotated_video', 'modified_frames']
}

/** A one-option select is not a choice and should not be shown as one. */
export function hasStrategyChoice(modelId: string): boolean {
  return strategiesForModel(modelId).length > 1
}

export function selectionForModel(
  modelId: string,
  currentStrategy: string,
): {
  model: string
  provider: GenerationProvider
  strategy: GuidanceStrategy
  mode: 1 | 2
} {
  const option = modelOption(modelId)
  const allowed = strategiesForModel(option.id)
  const strategy = allowed.includes(currentStrategy as GuidanceStrategy)
    ? currentStrategy as GuidanceStrategy
    : allowed[0]
  return {
    model: option.id,
    provider: option.provider,
    strategy,
    mode: strategy === 'modified_frames' ? 2 : 1,
  }
}
