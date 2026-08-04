/**
 * pipelineConsole — 管线操作台(StartConsole)纯逻辑(供 PipelineEditor 调用 + vitest)
 *
 * Jupyter「第一个 cell」心智:固定管线槽(view_state.startCommandId)在编辑器内直接
 * 填参数、点运行。参数表单由命令 params_schema(JSON Schema,经 brief 下发)驱动;
 * 默认值三层 merge:schema default → view_state.startArgs(固定参数)→ argsFromSelection
 * (选中实体字段注入,声明式字段映射,P-4 闭集)。
 * requires_runtime:start 命令在外部运行时缺位时返回空 patches op + 结构化 op.error
 * (COOKBOOK R4 正道;raise ZeError 路线降级为 footer toast),操作台原地渲缺位说明。
 */

import type { EntityDict } from '../types'
import type { CommandSpecBrief } from '../../02-footer'

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

/** params_schema.properties(无/坏形 → 空)。 */
export function paramProperties(spec?: CommandSpecBrief | null): Record<string, Record<string, unknown>> {
  const props = isRecord(spec?.params_schema) ? spec!.params_schema!.properties : undefined
  if (!isRecord(props)) return {}
  const out: Record<string, Record<string, unknown>> = {}
  for (const [k, v] of Object.entries(props)) if (isRecord(v)) out[k] = v
  return out
}

/** params_schema.required 的裸字段名(ParamPanel requiredFields 标注用)。 */
export function requiredParamNames(spec?: CommandSpecBrief | null): string[] {
  const req = isRecord(spec?.params_schema) ? spec!.params_schema!.required : undefined
  return Array.isArray(req) ? req.filter((x): x is string => typeof x === 'string') : []
}

function readEntityField(entity: EntityDict | null | undefined, field: string): unknown {
  if (!entity) return undefined
  if (field === 'id') return entity.id
  if (field === 'name') return entity.name
  if (field === 'type') return entity.type
  return entity.data?.[field]
}

/** 起跑参数草稿:schema default → startArgs → argsFromSelection(选中实体注入)。 */
export function buildStartDraft(
  spec: CommandSpecBrief | null | undefined,
  startArgs: Record<string, unknown>,
  argsFromSelection: Record<string, string>,
  selectedEntity: EntityDict | null,
): Record<string, unknown> {
  const draft: Record<string, unknown> = {}
  for (const [k, fs] of Object.entries(paramProperties(spec))) {
    if ('default' in fs) draft[k] = fs.default
  }
  Object.assign(draft, startArgs)
  for (const [arg, field] of Object.entries(argsFromSelection)) {
    const v = readEntityField(selectedEntity, field)
    if (v !== undefined) draft[arg] = v
  }
  // 默认把当前实体 id 注入 args.id(显式 startArgs/argsFromSelection 未设时)——管线作用于其
  // 绑定/选中的实体,需要 id 的 stage 处理器(doc.get(type, args["id"]))据此解析目标;create-on-start
  // 处理器忽略多余 arg。纯加性:显式提供 id 仍优先;无选中实体则不注入(行为同旧路径)。
  if (draft.id === undefined) {
    const sid = readEntityField(selectedEntity, 'id')
    if (sid !== undefined && sid !== null && sid !== '') draft.id = sid
  }
  return draft
}

/** required 缺值的参数名(显示名走 properties[k].label 回退裸名);非空数组 → Run 置灰。 */
export function missingRequiredParams(
  spec: CommandSpecBrief | null | undefined,
  draft: Record<string, unknown>,
): string[] {
  return requiredParamNames(spec)
    .filter((k) => {
      const v = draft[k]
      return v === undefined || v === null || (typeof v === 'string' && v.trim() === '')
    })
    .map((k) => {
      const label = paramProperties(spec)[k]?.label
      return typeof label === 'string' ? label : k
    })
}

/** 绑定 run 是否仍在活跃生命周期(固定管线一次一跑 → Run 置灰)。 */
export function runIsActive(status?: string | null): boolean {
  return status === 'running' || status === 'paused'
    || status === 'interrupting' || status === 'cancelling'
}

export type RequiresRuntime = { message: string; missing: string[]; hint: string }

/** dispatch 结果 → requires_runtime 结构(op.error.code==='requires_runtime');非则 null。 */
export function parseRequiresRuntime(result: unknown): RequiresRuntime | null {
  if (!isRecord(result)) return null
  const op = result.op
  if (!isRecord(op)) return null
  const err = op.error
  if (!isRecord(err) || err.code !== 'requires_runtime') return null
  return {
    message: typeof err.message === 'string' ? err.message : '',
    missing: Array.isArray(err.missing) ? err.missing.filter((x): x is string => typeof x === 'string') : [],
    hint: typeof err.hint === 'string' ? err.hint : '',
  }
}
