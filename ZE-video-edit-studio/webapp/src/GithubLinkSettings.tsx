/**
 * GithubLinkSettings — Header 设置区「GitHub 仓库」面板(系统 1b 关联入口)。
 *
 * 与 LocalLinkSettings 同处设置区:本地文件夹 + GitHub 仓库是「关联源」的两种。
 * 绑定 = 写 only:owner/repo/ref 走关联元数据,令牌(PAT)经 write-only 的
 * /api/account/credentials 密封保存,**绝不回显**(输入框 type=password、保存后清空、
 * 从不从服务端回填)。查关联只显示 owner/repo/ref + 是否已绑,不碰令牌。
 *
 * 文案走 05-i18n 的 githubLink 命名空间(translate(props.locale, ...));`c` 保持原形状,
 * 渲染逻辑不变(同 LocalLinkSettings 的约定)。
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react'

import {
  apiListCredentials,
  apiSaveCredential,
  apiDeleteCredential,
  type CredentialRecord,
} from '@ze/core/04-user-facing/03-authentication'
import { translate } from '@ze/i18n'

type Locale = 'zh' | 'en'

const PROVIDER = 'github_repo' as const

function metaStr(record: CredentialRecord, key: string): string {
  const value = record.metadata?.[key]
  return typeof value === 'string' ? value : ''
}

export type GithubLinkSettingsProps = {
  locale: Locale
  disabled: boolean
}

export function GithubLinkSettings(props: GithubLinkSettingsProps): ReactNode {
  const tr = (k: string, p?: Record<string, string | number>) =>
    translate(props.locale, `githubLink.${k}`, p)
  const c = {
    title: tr('title'),
    owner: tr('owner'),
    repo: tr('repo'),
    refPlaceholder: tr('refPlaceholder'),
    token: tr('token'),
    tokenHint: tr('tokenHint'),
    link: tr('link'),
    linking: tr('linking'),
    unlink: tr('unlink'),
    loginNeeded: tr('loginNeeded'),
    incomplete: tr('incomplete'),
    failed: tr('failed'),
    linkedTo: (repo: string) => tr('linkedTo', { repo }),
    refLabel: (ref: string) => tr('refLabel', { ref }),
  }
  const [binding, setBinding] = useState<CredentialRecord | null>(null)
  const [owner, setOwner] = useState('')
  const [repo, setRepo] = useState('')
  const [ref, setRef] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  const load = useCallback(async () => {
    try {
      const { credentials } = await apiListCredentials()
      const found = credentials.find((cred) => cred.provider === PROVIDER) ?? null
      setBinding(found)
    } catch {
      setBinding(null)
    }
  }, [])

  useEffect(() => {
    if (!props.disabled) void load()
  }, [props.disabled, load])

  const onLink = async () => {
    setFailed(false)
    if (!owner.trim() || !repo.trim() || !token.trim()) {
      setFailed(true)
      return
    }
    setBusy(true)
    try {
      await apiSaveCredential({
        provider: PROVIDER,
        label: `${owner.trim()}/${repo.trim()}`,
        secret: token,            // write-only:令牌只上行,绝不回显
        metadata: {
          owner: owner.trim(),
          repo: repo.trim(),
          ...(ref.trim() ? { ref: ref.trim() } : {}),
        },
      })
      setToken('')               // 清空,内存里不留令牌
      setOwner('')
      setRepo('')
      setRef('')
      await load()
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  const onUnlink = async () => {
    if (!binding) return
    setBusy(true)
    setFailed(false)
    try {
      await apiDeleteCredential(binding.id)
      await load()
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div data-ze-region="github-link">
      <div className="ze-text" data-size="xs" data-tone="secondary" style={{ padding: '4px 8px' }}>
        {c.title}
      </div>

      {props.disabled ? (
        <div className="ze-text" data-size="xs" data-tone="secondary" style={{ padding: '2px 8px' }}>
          {c.loginNeeded}
        </div>
      ) : binding ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '4px 8px' }}>
          <div className="ze-text" data-size="sm">
            {c.linkedTo(`${metaStr(binding, 'owner')}/${metaStr(binding, 'repo')}`)}
          </div>
          {metaStr(binding, 'ref') ? (
            <div className="ze-text" data-size="xs" data-tone="secondary">
              {c.refLabel(metaStr(binding, 'ref'))}
            </div>
          ) : null}
          <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                  disabled={busy} onClick={() => void onUnlink()}>
            {c.unlink}
          </button>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '4px 8px' }}>
          <input className="ze-input" data-size="sm" type="text" value={owner}
                 placeholder={c.owner} autoComplete="off"
                 onChange={(e) => setOwner(e.target.value)} />
          <input className="ze-input" data-size="sm" type="text" value={repo}
                 placeholder={c.repo} autoComplete="off"
                 onChange={(e) => setRepo(e.target.value)} />
          <input className="ze-input" data-size="sm" type="text" value={ref}
                 placeholder={c.refPlaceholder} autoComplete="off"
                 onChange={(e) => setRef(e.target.value)} />
          <input className="ze-input" data-size="sm" type="password" value={token}
                 placeholder={c.token} autoComplete="off"
                 onChange={(e) => setToken(e.target.value)} />
          <div className="ze-text" data-size="xs" data-tone="secondary">{c.tokenHint}</div>
          <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                  disabled={busy} onClick={() => void onLink()}>
            {busy ? c.linking : c.link}
          </button>
        </div>
      )}

      {failed ? (
        <div className="ze-text" data-size="xs" data-tone="danger" style={{ padding: '2px 8px' }}>
          {owner || repo || token ? c.incomplete : c.failed}
        </div>
      ) : null}
    </div>
  )
}
