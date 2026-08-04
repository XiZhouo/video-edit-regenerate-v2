/**
 * Footer — 对外入口
 *
 * 协议: ./SPEC.md
 * 视觉: 03-css-rules/04-editors/footer.css(在 FooterShell.tsx 顶部 import)
 *
 * layout 父级(01-layout/SPEC.md §2.1)装载 Footer 时:
 *   import { Footer } from './02-footer'
 *
 * mod 想自定义子组件 / 用法时,可单独引各子组件 / 类型:
 *   import { CollapsedBar, type FooterModOverrides } from './02-footer'
 */

export { FooterShell, FooterShell as Footer } from './FooterShell'

export { CollapsedBar } from './CollapsedBar'
export { OpLogPanel } from './OpLogPanel'
export { PipelineList } from './PipelineList'
export { DetailsTabs } from './DetailsTabs'

export type {
  FooterProps,
  FooterConfig,
  FooterModOverrides,
  ServerStatus,
  SyncState,
  PersistenceState,
  OpLogEntry,
  PipelineActiveItem,
  PipelineArtifact,
  PipelineLogEvent,
  PipelinePort,
  PipelineRun,
  PipelineStepState,
  CommandSpecBrief,
  PipelineStaticMeta,
  ErrorEntry,
  TraceLink,
} from './types'

export {
  FOOTER_COMMANDS,
  CMD_PIPELINE_PAUSE,
  CMD_PIPELINE_INTERRUPT,
  CMD_PIPELINE_RESUME,
  CMD_PIPELINE_CANCEL,
  CMD_PIPELINE_RETRY_FAILED,
  CMD_PIPELINE_RERUN_STEP,
  CMD_PIPELINE_RUN_FROM_STEP,
  type FooterCommand,
  type PipelineLifecyclePayload,
} from './commands'

export {
  FooterDispatchContext,
  useFooterDispatch,
  type DispatchFn,
} from './lib/dispatch'
