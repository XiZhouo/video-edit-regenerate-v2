/**
 * ProjectLinkDialog — 「项目关联」对话框(R-C17)。一个项目 = 0/1 本地文件夹 + 0/1 git 仓库。
 *
 * 三段:
 *  1. 本地文件夹(本设备):复用 LocalLinkSettings 渲染 + useLocalLink 透传 props(零逻辑迁移;
 *     句柄在浏览器 IndexedDB,物理上只属本设备 —— 段标题明示,对抗审 M4)。
 *  2. git 仓库(本项目,所有设备共用):owner/repo/ref → /api/projects/{id}/git-link;
 *     检测到旧账号级绑定且本项目未关联时,提供「迁入本项目」一键导入(不删旧绑定,fail-safe)。
 *  3. 访问令牌 PAT(账号级,所有项目共用):write-only 密封;仅一条 github_repo credential 时
 *     复用其 id 更新,多条时列出让用户自选,绝不静默猜(对抗审 C3)。
 *
 * 模式 B overlay(ImportProposalModal 先例):不动 core DialogKind;视觉全复用 ze-dialog 原子。
 * guest/demo:git 与 PAT 段禁用(服务端另有 ACL 兜底,对抗审 M6)。
 */

import { useEffect, useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing'
import {
  apiListCredentials, apiSaveCredential,
  apiGetProjectGitLink, apiPutProjectGitLink, apiDeleteProjectGitLink,
  type CredentialRecord, type ProjectGitLink,
} from '@ze/core/04-user-facing/03-authentication'

import { LocalLinkSettings } from './LocalLinkSettings'
import type { LocalLinkState, LocalLinkActions } from './lib/useLocalLink'

const PROVIDER = 'github_repo' as const

export function ProjectLinkDialog({ projectId, localLink, localDisabled, onExport, onImportFile, locale, guest, onClose }: {
  projectId: string
  localLink: LocalLinkState & LocalLinkActions
  localDisabled: boolean
  onExport: () => void
  onImportFile: (file: File) => void
  locale: 'zh' | 'en'
  guest: boolean
  onClose: () => void
}): ReactNode {
  const t = useT()
  const [link, setLink] = useState<ProjectGitLink | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [owner, setOwner] = useState('')
  const [repoName, setRepoName] = useState('')
  const [ref, setRef] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [creds, setCreds] = useState<CredentialRecord[]>([])
  const [token, setToken] = useState('')
  const [patTarget, setPatTarget] = useState<string>('')   // 多条时用户选中的 credential id

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        if (!guest) {
          const [linkResp, credResp] = await Promise.all([
            apiGetProjectGitLink(projectId),
            apiListCredentials(),
          ])
          if (!alive) return
          setLink(linkResp.git_link)
          if (linkResp.git_link) {
            setOwner(linkResp.git_link.owner)
            setRepoName(linkResp.git_link.repo)
            setRef(linkResp.git_link.ref)
          }
          setCreds(credResp.credentials.filter((c) => c.provider === PROVIDER))
        }
      } catch (e) {
        if (alive) setError((e as Error).message)
      } finally {
        if (alive) setLoaded(true)
      }
    })()
    return () => { alive = false }
  }, [projectId, guest])

  const saveGit = async () => {
    setBusy(true); setError('')
    try {
      const resp = await apiPutProjectGitLink(projectId, {
        owner: owner.trim(), repo: repoName.trim(),
        ...(ref.trim() ? { ref: ref.trim() } : {}),
      })
      setLink(resp.git_link)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const unlinkGit = async () => {
    setBusy(true); setError('')
    try {
      await apiDeleteProjectGitLink(projectId)
      setLink(null); setOwner(''); setRepoName(''); setRef('')
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  // 迁移:旧账号级绑定 metadata 有 owner/repo,而本项目未关联 → 一键迁入(不删旧绑定)
  const legacy = creds.find((c) => c.metadata?.owner && c.metadata?.repo)
  const migrate = async () => {
    if (!legacy) return
    setBusy(true); setError('')
    try {
      const resp = await apiPutProjectGitLink(projectId, {
        owner: String(legacy.metadata?.owner ?? ''),
        repo: String(legacy.metadata?.repo ?? ''),
        ...(legacy.metadata?.ref ? { ref: String(legacy.metadata.ref) } : {}),
      })
      setLink(resp.git_link)
      setOwner(resp.git_link.owner); setRepoName(resp.git_link.repo); setRef(resp.git_link.ref)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const bindPat = async () => {
    if (!token.trim()) return
    setBusy(true); setError('')
    try {
      // C3:恰一条时复用其 id 更新;多条时必须用户点选目标;零条时新建
      const id = creds.length === 1 ? creds[0].id
        : creds.length > 1 ? (patTarget || '')
        : undefined
      if (creds.length > 1 && !id) {
        setError(t('projectLink.pickPat'))
        return
      }
      await apiSaveCredential({
        ...(id ? { id } : {}), provider: PROVIDER,
        label: 'github', secret: token,
      })
      setToken('')
      const credResp = await apiListCredentials()
      setCreds(credResp.credentials.filter((c) => c.provider === PROVIDER))
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  const hasPat = creds.some((c) => c.status === 'active')

  return (
    <>
      <div className="ze-dialog__backdrop" onClick={onClose} />
      <div className="ze-dialog" data-open="true" data-size="lg" role="dialog" aria-modal="true">
        <div className="ze-dialog__header">
          <span className="ze-dialog__title">{t('projectLink.title')}</span>
        </div>
        <div className="ze-dialog__body ze-scroll-fade-y">
          <div className="ze-stack" data-gap="3">

            <div className="ze-stack" data-gap="1">
              <div className="ze-text" data-role="primary">
                {t('projectLink.localSection')}
                <span className="ze-text" data-role="secondary" data-size="sm"> · {t('projectLink.deviceScope')}</span>
              </div>
              <LocalLinkSettings
                {...localLink}
                locale={locale}
                disabled={localDisabled}
                onExport={onExport}
                onImportFile={onImportFile}
              />
            </div>

            <div className="ze-stack" data-gap="1">
              <div className="ze-text" data-role="primary">
                {t('projectLink.gitSection')}
                <span className="ze-text" data-role="secondary" data-size="sm"> · {t('projectLink.projectScope')}</span>
              </div>
              {guest ? (
                <div className="ze-text" data-role="secondary" data-size="sm">{t('projectLink.guestDisabled')}</div>
              ) : (
                <>
                  {link ? (
                    <div className="ze-text" data-size="sm">
                      {t('projectLink.gitLinkedTo')} <code>{link.owner}/{link.repo}</code>
                      {link.ref ? <> · {t('projectLink.gitRef')} <code>{link.ref}</code></> : null}
                    </div>
                  ) : null}
                  <div className="ze-gap" data-gap="1" style={{ display: 'flex', flexWrap: 'wrap' }}>
                    <input className="ze-input" data-size="sm" placeholder="owner" value={owner}
                           onChange={(e) => setOwner(e.target.value)} disabled={busy} />
                    <input className="ze-input" data-size="sm" placeholder="repo" value={repoName}
                           onChange={(e) => setRepoName(e.target.value)} disabled={busy} />
                    <input className="ze-input" data-size="sm" placeholder={t('projectLink.refPlaceholder')}
                           value={ref} onChange={(e) => setRef(e.target.value)} disabled={busy} />
                    <button type="button" className="ze-button" data-variant="primary" data-size="sm"
                            disabled={busy || !owner.trim() || !repoName.trim()} onClick={() => void saveGit()}>
                      {busy ? t('projectLink.saving') : t('projectLink.save')}
                    </button>
                    {link ? (
                      <button type="button" className="ze-button" data-size="sm" disabled={busy}
                              onClick={() => void unlinkGit()}>{t('projectLink.unlinkGit')}</button>
                    ) : null}
                  </div>
                  {!link && legacy && loaded ? (
                    <div className="ze-gap" data-gap="1" style={{ display: 'flex', alignItems: 'center' }}>
                      <span className="ze-text" data-role="secondary" data-size="sm">
                        {t('projectLink.migrateHint')} <code>{String(legacy.metadata?.owner)}/{String(legacy.metadata?.repo)}</code>
                      </span>
                      <button type="button" className="ze-button" data-size="sm" disabled={busy}
                              onClick={() => void migrate()}>{t('projectLink.migrate')}</button>
                    </div>
                  ) : null}
                </>
              )}
            </div>

            <div className="ze-stack" data-gap="1">
              <div className="ze-text" data-role="primary">
                {t('projectLink.patSection')}
                <span className="ze-text" data-role="secondary" data-size="sm"> · {t('projectLink.accountScope')}</span>
              </div>
              {guest ? (
                <div className="ze-text" data-role="secondary" data-size="sm">{t('projectLink.guestDisabled')}</div>
              ) : (
                <>
                  <div className="ze-text" data-role="secondary" data-size="sm">
                    {hasPat ? t('projectLink.patBound') : t('projectLink.patNotBound')}
                  </div>
                  {creds.length > 1 ? (
                    <div className="ze-stack" data-gap="0">
                      <div className="ze-text" data-role="secondary" data-size="sm">{t('projectLink.orphanWarning')}</div>
                      {creds.map((c) => (
                        <label key={c.id} className="ze-text" data-size="sm" style={{ display: 'flex', gap: 6 }}>
                          <input type="radio" name="pat-target" checked={patTarget === c.id}
                                 onChange={() => setPatTarget(c.id)} />
                          {c.label || c.id}{c.last4 ? ` (…${c.last4})` : ''}
                        </label>
                      ))}
                    </div>
                  ) : null}
                  <div className="ze-gap" data-gap="1" style={{ display: 'flex' }}>
                    <input className="ze-input" data-size="sm" type="password" autoComplete="off"
                           placeholder={t('projectLink.patToken')} value={token}
                           onChange={(e) => setToken(e.target.value)} disabled={busy} />
                    <button type="button" className="ze-button" data-size="sm"
                            disabled={busy || !token.trim()} onClick={() => void bindPat()}>
                      {busy ? t('projectLink.binding') : t('projectLink.bindPat')}
                    </button>
                  </div>
                </>
              )}
            </div>

            {error ? (
              <div className="ze-callout" data-variant="danger"><div className="ze-callout__body">{error}</div></div>
            ) : null}
          </div>
        </div>
        <div className="ze-dialog__footer">
          <button type="button" className="ze-button" data-size="sm" onClick={onClose}>
            {t('common.close')}
          </button>
        </div>
      </div>
    </>
  )
}
