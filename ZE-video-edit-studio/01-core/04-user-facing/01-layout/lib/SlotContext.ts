/**
 * SlotContext — 给 EditorSlot 透传它渲染所需的数据 + 回调
 *
 * 设计决策:选中状态是会话态,经本 context 透给 EditorSlot;EditorSlot 据 entity_bindings
 * 从这里取对应 entity_type 的 entities / selection / schema,并回调 onSelect / onAddFor。
 * 选择态本身由 L7 横向叶子 02-interaction/useInteractionStore 持有(M3 具名化),装配层
 * (webapp/App)调用该 hook 后 spread 进本 context —— 本 context 只管"透传",不持有状态。
 */

import { createContext, useContext, type ComponentType } from 'react'

import type {
  AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus,
  EntityTypeSchema, StatusLookup,
} from '../03-editor'
import type { CommandSpecBrief, PipelineRun, PipelineStaticMeta } from '../02-footer'
import type { MultiSelection, SelectMultiFn } from './selectionModel'
import type { AiRoutesState } from '../../03-authentication/aiRoutes'
import type { AiConsentState } from '../../03-authentication/aiConsent'

export type SlotContextValue = {
  /** 当前 user id;用于隔离个人视图偏好,不参与领域数据。 */
  userId?: string
  /** 当前 skill id;用于本地视图偏好 key,不参与领域数据。 */
  skillId?: string
  /** 当前 project id;用于本地视图偏好 key,不参与领域数据。 */
  projectId?: string
  /** 按 entity_type 分组的 entities(来自 useDocument)*/
  entitiesByType: Record<string, EntityDict[]>
  /** 每个 entity_type 当前选中的 id(会话态)= primary/active 指针(form/asset/canvas 读它)*/
  selection: Record<string, string | null>
  /** 选中某 entity_type 的某条(null = 清空)*/
  onSelect: (entityType: string, id: string | null) => void
  /** 多选集(加性,opt-in;table/gallery/task 用,单选编辑器忽略)。未提供 = 无多选。*/
  multiSelection?: MultiSelection
  /** 多选变更(replace/toggle/range);App 层 replace 时同步调 onSelect 让 active 指针跟随。*/
  onSelectMulti?: SelectMultiFn
  /** 跨类型最近选中(useInteractionStore.lastSelected 透传);form followSelection 槽读它。*/
  lastSelected?: { entityType: string; id: string } | null
  /** 共享时间游标/playhead(秒)= 与 selection 同级的第二条跨编辑器联动轴(C0,IState);
   *  timeline/preview/keyframe/transport/media 都读它,经本 context 透传(同 selection 路径)。
   *  未提供 = 装配层未接共享时间轴(seam 闭合,编辑器各自本地播放,不联动)。 */
  currentTime?: number
  /** 设共享时间游标(秒);任意编辑器写,绑同轴的其他编辑器据 currentTime 跟随(scrub→预览闭环)。*/
  setCurrentTime?: (t: number) => void
  /** 跨面板高亮通道(F2,Rendering Hints 层):每个 entity_type 当前高亮的 id 集
   *  (useInteractionStore.highlights 透传)。纯视觉会话态,与 selection 互不干扰、可叠加;
   *  未提供 = 装配层未接高亮通道(seam 闭合,编辑器不高亮)。 */
  highlights?: Record<string, string[]>
  /** 设某 entity_type 的高亮集([] 等价清除);任意面板写,消费面板(canvas2d spatial)据 highlights 绘制。*/
  setHighlights?: (entityType: string, ids: string[]) => void
  /** 清某 entity_type 的高亮;其余类型不动。*/
  clearHighlights?: (entityType: string) => void
  /** 每个 entity_type 的 form schema(来自 doc.data_schema)*/
  schemas: Record<string, EntityTypeSchema | undefined>
  /** 命令通道(POST /api/command;entity 状态由 SSE 回灌)*/
  dispatchCmd: DispatchFn
  /** 新增某 entity_type 的一条(默认字段是业务知识,装配层提供)*/
  onAddFor?: (entityType: string) => void
  /** canvas2d 拖入文件:上传需字节流 + asset API(非 JSON command 能承载),由装配层提供。
   *  不提供则 canvas2d 不接受拖入(seam 闭合,不伪造)。 */
  onDropFileFor?: (entityType: string, file: File) => void
  /** 唯一前端「写资产」接缝:上传二进制 → 内容寻址 asset_ref(装配层接 POST /api/asset)。
   *  mod 图元(W1a mask 提交 / 导图)拿 ref 后再走 dispatchCmd(JSON)落库。不提供则相关
   *  图元不接受上传(seam 闭合,不伪造)。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  /** 查某 entity 的运行态(占位/进度/错误);来自 useDocument 的 SSE entity_status。 */
  statusFor?: StatusLookup
  /** 当前 project 的 pipeline run 快照;pipeline editor 用。 */
  pipelineRuns?: PipelineRun[]
  /** 当前 project 的 command 静态声明;pipeline editor 用。 */
  commandSpecs?: CommandSpecBrief[]
  /** 可选 pipeline 静态声明;没有时从 run.stages / step_states 回退。 */
  pipelineSpecs?: PipelineStaticMeta[]
  /** asset 引用 → URL(gallery/canvas2d/scene3d/asset/media 用;editor 不直读 store)。 */
  assetUrl?: AssetUrlFn
  /** mod 注入的渲染后端(如 scene3d 的 three.js 组件);按 editor_class 取。 */
  renderers?: Record<string, ComponentType<any>>
  /** AI image 通道路线态(来自 useAiRoutes / GET /api/ai-routes);声明了 requires_route='image'
   *  的命令入口按钮据此在未绑凭据时置灰+「去绑定」(3.56「没绑就先说」不变量上生到 action)。
   *  可选、加性:未提供时装配层不接门控(seam 闭合,驱动层 428 仍兜底)。 */
  aiRoutes?: AiRoutesState
  /** AI 内容合规同意态(来自 useAiConsent / GET /api/ai-consent);声明了 requires_route 的命令入口
   *  在未接受当前版本条款时拦首击弹条款(AiConsentGate)。可选、加性:未提供或部署未配条款(version 空)
   *  时门不激活(seam 闭合,向后兼容)。与 aiRoutes 组合时条款门在外、路线门在内。 */
  aiConsent?: AiConsentState
}

export type { AssetRefDict, AssetUrlFn, EntityRunStatus, StatusLookup, PipelineRun, CommandSpecBrief, PipelineStaticMeta }
export type { MultiSelection, SelectMultiFn, SelectionMode } from './selectionModel'
export { applySelect, selectionModeFromEvent } from './selectionModel'

export const SlotContext = createContext<SlotContextValue | null>(null)

export function useSlotContext(): SlotContextValue {
  const v = useContext(SlotContext)
  if (!v) throw new Error('useSlotContext: SlotContext.Provider missing')
  return v
}
