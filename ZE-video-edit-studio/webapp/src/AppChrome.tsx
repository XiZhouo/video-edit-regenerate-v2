/**
 * AppChrome — Workspace 壳体的「chrome 组装」组合点(B3 从 App.tsx 拆出)。
 *
 * 角色:**受管 core 复制面**(webapp/src 顶层文件,随 Skill.Sync 整体下发;非 product 目录,
 * 下游不改本文件)。零业务知识、零自有状态:Header / 访客横幅 / 项目告警条 / 中区槽
 * (children)/ Footer / 错误浮层 / CopilotDock / GuideLayer 的框架 chrome 结构在此装配,
 * 全部数据与回调由 App.tsx(Workspace)注入。中区内容的路由 / mod renderers 注入是另一个
 * 组合点,见 AppRenderers.tsx。DOM 结构与拆分前的 App.tsx 逐节点一致(行为零变化)。
 */

import type { ComponentProps, ReactNode } from 'react'
import {
  HeaderShell, FooterShell, AssistantRoot, ZeIcon, useT,
} from '@ze/core/04-user-facing'
import { AmbientField } from './AmbientField'
import { GuideLayer } from './GuideLayer'

export type WorkspaceChromeProps = {
  skillId: string
  ambient: 'static' | 'field'
  header: ComponentProps<typeof HeaderShell>
  footer: ComponentProps<typeof FooterShell>
  guest: boolean
  onExitGuest?: () => void
  refreshError?: { message: string } | null
  projectMutationError?: { message: string } | null
  projectMutationIntents: readonly { state: string }[]
  errors: { id: string; message: string }[]
  onDismissError: (id: string) => void
  /** 运行时命名空间 key:变更即整体重挂 CopilotDock / GuideLayer(与拆分前同语义)。 */
  runtimeKey: string
  copilotProjectId: string | undefined
  copilotExpectedGeneration: number | undefined
  selection: Record<string, string | null>
  /** R-C18:数据结构损坏摘要 + 打开诊断卡回调(有损坏才显示诊断条)。 */
  schemaDiagnosis?: { finding_count: number } | null
  onOpenDiagnosis?: () => void
  /** 覆盖层(如导入提案卡);渲染位置在错误浮层与 CopilotDock 之间,保持既有 DOM 顺序。 */
  dialogs?: ReactNode
  children: ReactNode
}

export function WorkspaceChrome({
  skillId, ambient, header, footer, guest, onExitGuest,
  refreshError, projectMutationError, projectMutationIntents,
  schemaDiagnosis, onOpenDiagnosis,
  errors, onDismissError, runtimeKey, copilotProjectId, copilotExpectedGeneration,
  selection, dialogs, children,
}: WorkspaceChromeProps) {
  const t = useT()
  return (
    <div className="ze-app" data-mod={skillId} data-ze-ambient={ambient}>
      {ambient === 'field' ? <AmbientField /> : null}
      <HeaderShell {...header} />

      {guest && (
        <div data-ze-region="guest-banner">
          <span className="ze-text" data-size="sm" data-tone="secondary">{t('auth.guestSaveHint')}</span>
          <button className="ze-button" data-variant="primary" type="button"
                  onClick={() => onExitGuest?.()}>{t('auth.guestLogin')}</button>
        </div>
      )}

      {(refreshError || projectMutationError || projectMutationIntents.length > 0) ? (
        <div className="ze-callout" data-variant="warning" role="status" aria-live="polite">
          {projectMutationError
            ? projectMutationError.message
            : projectMutationIntents.some((intent) => intent.state === 'repair_required')
              ? t('auth.projectMutation.repairRequired')
              : projectMutationIntents.length > 0
                ? t('auth.projectMutation.stillReconciling')
                : refreshError?.message}
        </div>
      ) : null}

      {schemaDiagnosis && schemaDiagnosis.finding_count > 0 && onOpenDiagnosis ? (
        <div className="ze-callout" data-variant="warning" role="status" aria-live="polite">
          <span>{t('schemaDoctor.bannerPrefix')} {schemaDiagnosis.finding_count} {t('schemaDoctor.bannerSuffix')}</span>
          <button className="ze-button" data-size="sm" type="button"
                  onClick={onOpenDiagnosis}>{t('schemaDoctor.viewBanner')}</button>
        </div>
      ) : null}

      <main className="ze-app__main">
        {children}
      </main>

      <FooterShell {...footer} />

      <ErrorToasts errors={errors} onDismiss={onDismissError} />

      {dialogs}

      {/* 单一 AI 助手(W1-1):**一个根**,精灵与对话面板同属它。
          此前这里是 <CopilotDock> 与 <GuideLayer> 两个并列兄弟——两棵互不相干的树,
          于是面板只能凭空出现在固定位置,而不是从精灵身上长出来;用户判词
          「聊天框是它的分身」说的就是这个结构。现在精灵作为 sprite 传进根里,
          它播的停泊坐标由根写成 --ze-assistant-origin-*,面板据此定 transform-origin。 */}
      <AssistantRoot
        key={runtimeKey}
        projectId={copilotProjectId}
        expectedGeneration={copilotExpectedGeneration}
        selection={selection}
        sprite={
          <GuideLayer
            key={`guide:${runtimeKey}`}
            projectId={copilotProjectId}
            selection={selection}
            expectedGeneration={copilotExpectedGeneration}
          />
        }
      />
    </div>
  )
}

/**
 * ErrorToasts — 命令失败的即时可见层(框架级)。footer ERRORS 留作历史/审计,
 * 但 footer 常折叠,用户点了命令失败需要"当场看见"——故在右下角浮最近几条,可手动关。
 * 复用 03-css-rules 的 ze-toast 视觉壳(本就是为「出现/消失时机=React 运行时」留的),
 * 这里补上其运行时交互;只用已定义的 .ze-toast* 类,过 css-contract。
 */
function ErrorToasts({ errors, onDismiss }: {
  errors: { id: string; message: string }[]
  onDismiss: (id: string) => void
}) {
  const t = useT()
  const shown = errors.slice(0, 3)   // 只浮最近 3 条;更早的去 footer ERRORS 看
  if (shown.length === 0) return null
  return (
    <div className="ze-toast__viewport" role="alert" aria-live="assertive">
      {shown.map((e) => (
        <div key={e.id} className="ze-toast" data-variant="danger">
          <div className="ze-toast__content">
            <div className="ze-toast__body">{e.message}</div>
          </div>
          <button className="ze-toast__close" onClick={() => onDismiss(e.id)}
                  aria-label={t('common.close')}><ZeIcon name="close" size="xs" /></button>
        </div>
      ))}
    </div>
  )
}
