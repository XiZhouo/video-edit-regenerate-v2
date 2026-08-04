/**
 * ProjectPicker — 登录后未打开项目时的中区空态:列可见项目(自己的 + 被共享的)+ 新建。
 *
 * 显示在工作台中区(header 仍在,可登出/切号)。选一个 → openProject → useDocument 加载;
 * 新建 → createProject(owner=me)+ 可选示例数据。
 * 样式走 @css/04-editors/auth.css 的 .ze-project-picker*(只用 --ze-* token)。
 */

import { useRef, useState, type FormEvent } from 'react'
import { useProject } from './ProjectContext'
import { useAuth } from './AuthContext'
import { apiRequestEditPermission } from './authApi'
import type { ProjectMutationIntent } from './projectMutationIntent'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import {
  OperationManualContent,
  useOperationManual,
} from '@ze/core/04-user-facing/01-layout/lib/operationManual'
import { useScrollAffordance } from '@ze/core/04-user-facing/01-layout/03-editor/lib/useScrollAffordance'

import '@ze/css/05-compositions/access/auth/code.css'

type ProjectNameSource = { name: string }
type ProjectUserNameSource = { id?: string; name?: string; email?: string }

export type ProjectPickerSafetyState = {
  hasFrozenMutation: boolean
  hasReconcilingMutation: boolean
}

/**
 * Collapse any number of durable intents into two user-facing safety states.
 * A consumed replay is as frozen as repair_required after reload: both may
 * reconcile proven evidence, but neither is allowed to issue another write.
 */
export function projectPickerSafetyState(
  intents: Pick<ProjectMutationIntent, 'state'>[],
): ProjectPickerSafetyState {
  return {
    hasFrozenMutation: intents.some(({ state }) => (
      state === 'repair_required' || state === 'replay_in_flight'
    )),
    hasReconcilingMutation: intents.some(({ state }) => (
      state !== 'repair_required' && state !== 'replay_in_flight'
    )),
  }
}

type ProjectPickerSafetyNoticesProps = {
  mutationSafety: ProjectPickerSafetyState
  projectMutationError: Error | null
  refreshError: Error | null
  t: ReturnType<typeof useT>
}

/** Visible, non-actionable evidence for writes whose terminal state is unknown. */
export function ProjectPickerSafetyNotices({
  mutationSafety, projectMutationError, refreshError, t,
}: ProjectPickerSafetyNoticesProps) {
  return (
    <>
      {projectMutationError && (
        <div className="ze-project-picker__error" role="alert">
          {projectMutationError.message}
        </div>
      )}
      {mutationSafety.hasFrozenMutation && (
        <div className="ze-project-picker__error" role="alert">
          {t('auth.projectMutation.repairRequired')}
        </div>
      )}
      {mutationSafety.hasReconcilingMutation && (
        <div className="ze-project-picker__hint" role="status">
          {t('auth.projectMutation.stillReconciling')}
        </div>
      )}
      {refreshError && (
        <div className="ze-project-picker__hint" role="status" data-state="snapshot-retained">
          {t('auth.projectMutation.refreshSnapshotRetained', { message: refreshError.message })}
        </div>
      )}
    </>
  )
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function defaultProjectName(
  user: ProjectUserNameSource | null | undefined,
  projects: ProjectNameSource[],
  fallbackBase: string,
): string {
  const base = (user?.name || user?.email || user?.id || fallbackBase).trim() || fallbackBase
  const namePattern = new RegExp(`^${escapeRegExp(base)}(?:\\s+(\\d+))?$`)
  let maxSequence = 0

  for (const project of projects) {
    const match = project.name.trim().match(namePattern)
    if (!match) continue
    const sequence = match[1] ? Number(match[1]) : 1
    if (Number.isFinite(sequence)) maxSequence = Math.max(maxSequence, sequence)
  }

  return `${base} ${maxSequence + 1}`
}

export function ProjectPicker({ manualHtml, onImportProject, onLinkGithub }: {
  manualHtml?: string
  /** 导入项目(选文件 → App 新建项目 + 把文件导进去);缺省不渲导入入口。 */
  onImportProject?: (file: File) => void | Promise<void>
  /** 关联 GitHub(App 新建项目 + 打开项目关联对话框);缺省不渲 GitHub 入口。 */
  onLinkGithub?: () => void | Promise<void>
} = {}) {
  const t = useT()
  const { user, refreshUser } = useAuth()
  const {
    projects, loading, refreshError,
    projectMutationIntents, projectMutationError,
    openProject, createProject, demoTemplates, openDemoTemplate,
  } = useProject()
  const mutationSafety = projectPickerSafetyState(projectMutationIntents)
  const canEdit = user?.permission === 'edit' || user?.is_super_admin
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')
  const importInputRef = useRef<HTMLInputElement>(null)
  const pickerRef = useScrollAffordance('y')
  // 与 Header ManualDialog 共用唯一加载/净化/渲染边界。片段只能提供
  // `.ze-om*` 结构；CSS、脚本与事件属性均由该边界剥离。
  const operationManual = useOperationManual(manualHtml)

  const create = async (e: FormEvent) => {
    e.preventDefault()
    if (!canEdit || busy) return
    const projectName = name.trim() || defaultProjectName(user, projects, t('auth.guestName'))
    setBusy(true)
    setErr('')
    setNotice('')
    try {
      await createProject(projectName, false)
      setName('')
    } catch (e2) {
      setErr((e2 as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const openDemo = async (id: string) => {
    if (busy) return
    const expectedGeneration = demoTemplates.find((template) => template.id === id)?.sandbox_generation
    if (!Number.isSafeInteger(expectedGeneration) || (expectedGeneration as number) < 0) {
      setErr(t('auth.projectMutation.generationRequired'))
      return
    }
    if ((expectedGeneration as number) > 0
        && !globalThis.confirm(t('auth.projects.demoResetConfirm'))) return
    setBusy(true)
    setErr('')
    setNotice('')
    try {
      await openDemoTemplate(id, expectedGeneration as number)
    } catch (e2) {
      setErr((e2 as Error).message)
    } finally {
      setBusy(false)
    }
  }

  // 第一触点完整项目入口:导入项目 / 关联 GitHub(与新建/打开/示例并列,不必翻文件切换器菜单)。
  const runEntry = async (fn: () => void | Promise<void>) => {
    if (!canEdit || busy) return
    setBusy(true); setErr(''); setNotice('')
    try { await fn() } catch (e2) { setErr((e2 as Error).message) } finally { setBusy(false) }
  }
  const importProject = (file: File) => runEntry(() => onImportProject!(file))
  const linkGithub = () => runEntry(() => onLinkGithub!())

  const requestEdit = async () => {
    if (busy) return
    setBusy(true)
    setErr('')
    setNotice('')
    try {
      const res = await apiRequestEditPermission('project-picker')
      if (res.status === 'already_granted') await refreshUser()
      setNotice(res.status === 'already_granted'
        ? t('auth.permission.alreadyGranted')
        : t('auth.permission.requestSent'))
    } catch (e2) {
      setErr((e2 as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const leftContent = (
    <>
      <div className="ze-project-picker__title">{t('auth.projects.title')}</div>

      <form onSubmit={create} className="ze-project-picker__new">
        <input className="ze-input" style={{ flex: 1, minWidth: 0 }} placeholder={t('auth.projects.newPlaceholder')} value={name}
               onChange={(e) => setName(e.target.value)} disabled={!canEdit} />
        <button className="ze-button" data-variant="primary" type="submit" disabled={busy || !canEdit}>{t('auth.projects.create')}</button>
      </form>

      {(onImportProject || onLinkGithub) && (
        <div className="ze-row" data-gap="2" data-wrap="true">
          {onImportProject && (
            <>
              <button type="button" className="ze-button" data-size="sm" disabled={busy || !canEdit}
                      onClick={() => importInputRef.current?.click()}>
                {t('auth.projects.import')}
              </button>
              <input ref={importInputRef} type="file" accept=".json,.zip,application/json,application/zip"
                     hidden disabled={busy || !canEdit}
                     onChange={(e) => { const f = e.target.files?.[0]; if (f) void importProject(f); e.target.value = '' }} />
            </>
          )}
          {onLinkGithub && (
            <button type="button" className="ze-button" data-size="sm" disabled={busy || !canEdit}
                    onClick={() => void linkGithub()}>
              {t('auth.projects.linkGithub')}
            </button>
          )}
        </div>
      )}
      {!canEdit && (
        <div className="ze-project-picker__hint">
          {t('auth.permission.readOnlyHint')}{' '}
          <button className="ze-button" data-size="sm" type="button" onClick={() => void requestEdit()} disabled={busy}>
            {busy ? t('auth.busy') : t('auth.permission.requestEdit')}
          </button>
        </div>
      )}
      {err && <div className="ze-project-picker__error" role="alert">{err}</div>}
      <ProjectPickerSafetyNotices
        mutationSafety={mutationSafety}
        projectMutationError={projectMutationError?.message === err ? null : projectMutationError}
        refreshError={refreshError}
        t={t}
      />
      {notice && <div className="ze-project-picker__hint" role="status">{notice}</div>}

      {demoTemplates.length > 0 && (
        <div className="ze-project-picker__demos">
          <div className="ze-project-picker__section-label">{t('auth.projects.examplesTitle')}</div>
          <div className="ze-project-picker__demo-list">
            {demoTemplates.map((d) => (
              <button key={d.id} type="button" className="ze-project-picker__demo"
                      onClick={() => void openDemo(d.id)} disabled={busy}>
                <div className="ze-project-picker__demo-name">{d.name}</div>
                {d.description && <div className="ze-project-picker__demo-desc">{d.description}</div>}
                {d.business_scene && <div className="ze-project-picker__demo-scene">{d.business_scene}</div>}
              </button>
            ))}
          </div>
          <div className="ze-project-picker__hint">{t('auth.projects.examplesHint')}</div>
        </div>
      )}

      {loading ? (
        <div className="ze-project-picker__hint">{t('common.loading')}</div>
      ) : projects.length === 0 ? (
        <div className="ze-project-picker__hint">{t('auth.projects.empty')}</div>
      ) : (
        <div className="ze-project-picker__list">
          {projects.map((p) => (
            <div key={p.id} className="ze-project-picker__row" onClick={() => openProject(p.id)}>
              <div style={{ minWidth: 0 }}>
                <div className="ze-project-picker__row-name">{p.name}</div>
                <div className="ze-project-picker__row-meta">
                  {p.is_owner ? t('auth.projects.mine') : t('auth.projects.sharedBy', { owner: p.owner })}
                  {p.collaborators.length > 0 && t('auth.projects.collaboratorSuffix', { count: p.collaborators.length })}
                  {p.gitLinked && <> · {t('auth.projects.badgeGit')}</>}
                </div>
              </div>
              <div className="ze-project-picker__row-open">{t('auth.projects.open')}</div>
            </div>
          ))}
        </div>
      )}
    </>
  )

  // 有真操作说明片段 → 双栏(左示例/项目栏 + 右说明全文,各自软滚动);无 → 维持单栏选示例(降级)。
  // 外层 .ze-project-picker-split 是硬 viewport 框(overflow hidden,不参与软滚动);两栏各自叠滚动渐变。
  if (operationManual.fragment) {
    return (
      <div className="ze-project-picker-split">
        <div className="ze-project-picker ze-scroll-fade-y" ref={pickerRef}>{leftContent}</div>
        <OperationManualContent
          className="ze-project-picker__manual ze-scroll-fade-y"
          fragment={operationManual.fragment}
        />
      </div>
    )
  }
  return (
    <div className="ze-project-picker ze-scroll-fade-y" ref={pickerRef}>
      {leftContent}
    </div>
  )
}
