/**
 * Footer 触发的 command_id 字符串常量
 *
 * SPEC: ./SPEC.md §2.7 跟其他叶子的接口
 *
 * **红线**(SPEC §2.6 + GATE-UI-FOOTER-*):Footer 只允许 dispatch 以下
 * pipeline lifecycle command,不允许 dispatch 任何业务数据 mutation。
 * 业务命令归 Header / editor toolbar。
 */

// ============================================================
// Pipeline lifecycle
// ============================================================

/** 暂停 pipeline */
export const CMD_PIPELINE_PAUSE = 'pipeline.pause'
/** 请求中断当前 stage,可继续 */
export const CMD_PIPELINE_INTERRUPT = 'pipeline.interrupt'
/** 继续 pipeline(从 paused 恢复)*/
export const CMD_PIPELINE_RESUME = 'pipeline.resume'
/** 取消 pipeline(终止,不可恢复)*/
export const CMD_PIPELINE_CANCEL = 'pipeline.cancel'
/** 统一重试失败项(只重跑 error 标记的 item;可选 item_ids 限定单项)*/
export const CMD_PIPELINE_RETRY_FAILED = 'pipeline.retry_failed'
/** 重跑某个 step */
export const CMD_PIPELINE_RERUN_STEP = 'pipeline.rerun_step'
/** 从某个 step 继续跑到最后 */
export const CMD_PIPELINE_RUN_FROM_STEP = 'pipeline.run_from_step'


// ============================================================
// 全集 — Footer 允许 dispatch 的 command 集合
//
// audit gate(GATE-UI-FOOTER-NO-BUSINESS-MUTATION)校验:
//   Footer dispatch 出去的 command_id 必须在本列表里;否则拒。
//   retry_failed 属管线生命周期(只重跑失败项),非业务 mutation,故在白名单内。
// ============================================================

export const FOOTER_COMMANDS = [
  CMD_PIPELINE_PAUSE,
  CMD_PIPELINE_INTERRUPT,
  CMD_PIPELINE_RESUME,
  CMD_PIPELINE_CANCEL,
  CMD_PIPELINE_RETRY_FAILED,
  CMD_PIPELINE_RERUN_STEP,
  CMD_PIPELINE_RUN_FROM_STEP,
] as const

export type FooterCommand = (typeof FOOTER_COMMANDS)[number]


// ============================================================
// dispatch payload 类型
// ============================================================

export type PipelineLifecyclePayload = {
  /** 目标 pipeline 实例 id(对应 PipelineRun.id)*/
  pipeline_id: string
  /** 仅 retry_failed:限定重试的 item_ids(不传 = 全部失败项)*/
  item_ids?: string[]
  /** rerun_step / run_from_step:目标 item(不传 = fan-out 全部 item)*/
  item_id?: string
  /** rerun_step / run_from_step:目标 stage */
  stage_id?: string
}
