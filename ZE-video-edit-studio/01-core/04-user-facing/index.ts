/**
 * 04-user-facing — core 前端稳定门面(barrel;B3 门面收口,core 3.54.0 新增)。
 *
 * 角色:webapp 装配层(App.tsx / AppChrome.tsx / AppRenderers.tsx)与下游
 * 02-mod/05-renderers.tsx 的**稳定 import 面**。core 内部目录日后重排时,只要保持
 * 本门面导出不变,消费方 import 就不断——消除「core 目录任何重排 = 全舰队断
 * import 的 MAJOR」脆弱面(四核心整理调查 inv_build.json 盘点 4)。
 *
 * 收录纪律(按需导出,非全量 re-export):
 *   只收录 App.tsx 与全舰队 02-mod/05-renderers.tsx 实际 import 的符号
 *   (2026-07 B3 普查清单),刻意不用 `export *`(星号聚合会静默吞并同名冲突)。
 *   新消费需求出现时在此显式追加一行 = core MINOR 加性变更。
 *
 * 兼容:旧深路径(如 `@ze/core/04-user-facing/01-layout/03-editor`)全部继续可用,
 * 本门面纯增量、不移动/不删除任何既有文件或导出。
 *
 * 用法:`import { EditorFrame, useT, type CustomPaneProps } from '@ze/core/04-user-facing'`
 */

// ── 01-layout:布局装配(LayoutTree 分屏 / slot 上下文 / 跨编辑器选择通道) ──
export {
  LayoutRenderer, SlotContext, useSlotContext, usePersistedLayout, loadLayout,
  useEditorSelection,
} from './01-layout'
export type {
  SlotContextValue, EditorSelectionApi, LayoutNode,
} from './01-layout'

// ── chrome:Header / Footer / 悬浮 AI 协作挂件 ──
export { HeaderShell } from './01-layout/01-header'
export { FooterShell } from './01-layout/02-footer'
export { CopilotDock, AssistantRoot, publishAssistant, subscribeAssistant } from './01-layout/copilot'
export type { AssistantFrame } from './01-layout/copilot'

// ── 03-editor:编辑器基座 / 功能区 zone / 空间基元 / 命令派发 ──
export {
  EditorDispatchContext, EditorFrame, ErrorPlaceholder,
  HeaderZone, FunctionZone, StatusZone, ActionBarZone, ToolbarZone,
  CanvasToolbar, ThumbnailGrid, CompareFrame,
  SpatialCanvas, useViewport, spatialMath, DEFAULT_CONSTRAINTS,
  AiCandidateDialog, normalizeScene3dCameraSnapshot,
} from './01-layout/03-editor'
export type {
  DispatchFn, EntityTypeSchema, FormActionResultConsumer,
  CustomPaneProps, EntityDict, AssetRefDict,
  Scene3dCameraChangeMeta, Scene3dCameraSnapshot, Scene3dRendererProps,
  SpatialBox, StatusSegment, ToolId,
} from './01-layout/03-editor'

// ── 03-editor 深叶(此前只有深路径可达;经门面收口) ──
export { useScrollAffordance } from './01-layout/03-editor/lib/useScrollAffordance'
export { AssetRefInput } from './01-layout/03-editor/primitives/AssetRefInput'
export { ZeIcon } from './01-layout/lib/zeIcon'

// ── 02-interaction:会话级交互态 ──
export { useInteractionStore } from './02-interaction'

// ── 03-authentication:账号 / 项目 ──
export {
  AuthProvider, useAuth, ProjectProvider, useProject, LoginPage, ProjectPicker,
} from './03-authentication'
export type { AuthUser } from './03-authentication'

// ── 05-preferences:主题 / 语言 / mod 词典注入 ──
export { PreferencesProvider, usePreferences, setModMessages, useT } from './05-preferences'

// ── AI 通道层(3.56):路线可用性 + 「没绑就先说」门控 ──
export {
  useAiRoutes,
  apiGetAiRoutes,
  requestOpenCredentials,
  notifyCredentialsChanged,
  CREDENTIALS_CHANGED_EVENT,
} from './03-authentication/aiRoutes'
export {
  AI_SETTINGS_PAGES, AI_SETTINGS_HASH_ROUTE, buildAiSettingsHash, parseAiSettingsHash,
  aiSettingsUrl, targetFromRouteFix, isAiSettingsTarget,
  type AiSettingsPage, type AiSettingsTarget,
} from './03-authentication/aiSettingsLink'
export type { AiRoute, AiRoutesState, AiRouteReason, AiRouteFix } from './03-authentication/aiRoutes'
export { AiRouteGate } from './01-layout/AiRouteGate'
// ── AI 内容合规同意(对外公测门:首次生成前接受条款;未配 ze.ai.terms_* 时不激活)──
export { useAiConsent, apiGetAiConsent } from './03-authentication/aiConsent'
export type { AiConsentState, AiConsentTerms } from './03-authentication/aiConsent'
export { AiConsentGate } from './01-layout/AiConsentGate'
