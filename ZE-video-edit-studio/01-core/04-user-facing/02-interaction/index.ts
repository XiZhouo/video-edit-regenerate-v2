/**
 * 02-interaction — 对外入口(L7 横向交互叶子)
 *
 * 协议: ./SPEC.md。当前导出会话级交互态 hook;其余横向规则(反馈线/进度/取消…)
 * 由各 UI 层就地遵守,不在此处建重型 store。
 */

export { useInteractionStore, type InteractionStore } from './useInteractionStore'
