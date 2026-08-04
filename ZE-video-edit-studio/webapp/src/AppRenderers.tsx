/**
 * AppRenderers — Workspace 壳体的「路由 / mod renderers 注入」组合点(B3 从 App.tsx 拆出)。
 *
 * 角色:**受管 core 复制面**(webapp/src 顶层文件,随 Skill.Sync 整体下发;非 product 目录,
 * 下游不改本文件)。两件事:
 *   1. MOD_RENDERERS — mod 注入式 custom 面板的稳定读取点(import.meta.glob 缝,原样迁自 App.tsx);
 *   2. WorkspaceMain — 中区二态路由:未开项目显 ProjectPicker,开了项目经 SlotContext 渲 LayoutRenderer。
 * chrome 组装是另一个组合点,见 AppChrome.tsx。DOM 结构与拆分前的 App.tsx 一致(行为零变化)。
 */

import {
  LayoutRenderer, SlotContext, ProjectPicker,
  type SlotContextValue, type LayoutNode, type FormActionResultConsumer,
} from '@ze/core/04-user-facing'

// mod 注入式 custom 面板:可选 02-mod/05-renderers.{ts,tsx},导出 `renderers: Record<string, ComponentType>`。
// 用 import.meta.glob(非硬 import)以兼容「没有该文件的 skill」——缺失即空表,slot custom 会显「未找到注入面板」。
// 这是框架标准 seam:mod 渲染器留在 02-mod(survive Skill.Sync),本文件只提供一个稳定读取点。
const MOD_RENDERER_MODULES = import.meta.glob('../../02-mod/05-renderers.{ts,tsx}', { eager: true }) as Record<
  string, { renderers?: Record<string, import('react').ComponentType<any>> }
>
export const MOD_RENDERERS: Record<string, import('react').ComponentType<any>> =
  Object.values(MOD_RENDERER_MODULES)[0]?.renderers ?? {}

export type WorkspaceMainProps = {
  projectId: string | null
  /** 文档元数据已就绪(meta 非空)才渲 LayoutRenderer;与拆分前 `meta ? …` 同语义。 */
  documentLoaded: boolean
  slotValue: SlotContextValue
  layout: LayoutNode
  onLayoutChange: (tree: LayoutNode) => void
  formActionResultConsumer: FormActionResultConsumer
  manualHtml?: string
  /** 落地页第一触点项目入口(App 注入):导入项目 / 关联 GitHub。 */
  onImportProject?: (file: File) => void | Promise<void>
  onLinkGithub?: () => void | Promise<void>
}

export function WorkspaceMain({
  projectId, documentLoaded, slotValue, layout, onLayoutChange,
  formActionResultConsumer, manualHtml, onImportProject, onLinkGithub,
}: WorkspaceMainProps) {
  return projectId ? (
    <SlotContext.Provider value={slotValue}>
      {documentLoaded ? (
        <LayoutRenderer
          tree={layout}
          onChange={onLayoutChange}
          formActionResultConsumer={formActionResultConsumer}
        />
      ) : null}
    </SlotContext.Provider>
  ) : (
    <ProjectPicker manualHtml={manualHtml} onImportProject={onImportProject} onLinkGithub={onLinkGithub} />
  )
}
