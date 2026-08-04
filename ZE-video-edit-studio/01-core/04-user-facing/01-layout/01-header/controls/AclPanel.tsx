/**
 * AclPanel — 头像菜单内嵌的成员/共享面板(header-v2,原 ShareButton 对话框逻辑收编)
 *
 * SPEC: ../SPEC.md §2.3.9(头像菜单组 2)
 *
 * 人人可见成员列表(owner 徽记 + collaborators);owner 额外渲染添加行
 * (输入 + datalist autocomplete)与每行移除钮 → dispatch `document.set_acl`
 * (经 Operation 广播,SSE meta.acl 回流 → props.acl 实时更新)。授权变更必须携带
 * 当前已渲染 Document version；协作者并发更新后由后端 CAS 拒绝，前端再取 canonical
 * snapshot，绝不以 last-write-wins 恢复旧 ACL。
 *
 * 形态:菜单内 role=group 区块(非 BaseDialog —— 原 ShareButton 也是自管轻卡片,
 * 收进 MenuPopover 后由 [data-wide] 放宽宽度);全部行内钮带 aria-label。
 */

import { useEffect, useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useHeaderDispatch } from '../lib/dispatch'
import { ZeIcon } from '../../lib/zeIcon'
import { CMD_DOCUMENT_SET_ACL } from '../commands'
import { apiLookupUsers, UserPicker, type ZeUserCard } from '../../../03-authentication'

export type ShareAcl = { owner?: string; collaborators?: string[] }

export type AclPanelProps = {
  acl: ShareAcl
  currentUserId: string
  /** 当前画布已经渲染的 Document version；document.set_acl 的 CAS 基线。 */
  documentVersion: number
}

export function AclPanel(props: AclPanelProps): ReactNode {
  const t = useT()
  const dispatch = useHeaderDispatch()
  const [selectedUserId, setSelectedUserId] = useState('')
  const [cards, setCards] = useState<Record<string, ZeUserCard>>({})
  const [err, setErr] = useState('')

  const owner = props.acl.owner ?? ''
  const collaborators = props.acl.collaborators ?? []
  const isOwner = !!props.currentUserId && props.currentUserId === owner
  const memberIds = [owner, ...collaborators].filter((u): u is string => typeof u === 'string' && u !== '')
  const memberKey = memberIds.join('|')

  useEffect(() => {
    const ids = memberKey ? memberKey.split('|') : []
    if (!ids.length) {
      setCards({})
      return
    }
    let cancelled = false
    apiLookupUsers(ids)
      .then((r) => {
        if (cancelled) return
        const next: Record<string, ZeUserCard> = {}
        for (const u of r.users) next[u.id] = u
        setCards(next)
      })
      .catch(() => {
        if (!cancelled) setCards({})
      })
    return () => { cancelled = true }
  }, [memberKey])

  const memberLabel = (userId: string): string => {
    const u = cards[userId]
    return u ? (u.name || u.email || u.id) : userId
  }

  const setAcl = (collabs: string[]) => {
    dispatch(CMD_DOCUMENT_SET_ACL, {
      acl: { owner, collaborators: collabs },
      expected_version: props.documentVersion,
    })
  }

  const add = () => {
    const u = selectedUserId.trim()
    setErr('')
    if (!u) return
    if (u === owner) { setErr(t('share.errSelf')); return }
    if (collaborators.includes(u)) { setErr(t('share.errExists')); return }
    setAcl([...collaborators, u])
    setSelectedUserId('')
  }

  const remove = (u: string) => setAcl(collaborators.filter((c) => c !== u))

  return (
    <div className="ze-header__acl-panel" role="group" aria-label={t('share.members')}>
      <div className="ze-header__acl-title">
        <ZeIcon name="share" size="xs" /> {t('share.members')}
      </div>

      <div className="ze-header__acl-member">
        <span className="ze-header__acl-member-badge" aria-hidden="true">{t('share.ownerBadge')}</span>
        <span className="ze-header__acl-member-name" title={owner || undefined}>{owner ? memberLabel(owner) : '—'}</span>
      </div>

      {collaborators.length === 0 ? (
        <div className="ze-header__acl-title">{t('share.empty')}</div>
      ) : collaborators.map((u) => (
        <div key={u} className="ze-header__acl-member">
          <span className="ze-header__acl-member-name" title={u}>{memberLabel(u)}</span>
          {isOwner && (
            <button
              type="button"
              className="ze-header__acl-remove"
              aria-label={`${t('share.remove')} — ${u}`}
              title={t('share.remove')}
              onClick={() => remove(u)}
            >
              <ZeIcon name="close" size="xs" />
            </button>
          )}
        </div>
      ))}

      {isOwner && (
        <>
          <div className="ze-header__acl-add-row">
            <UserPicker
              value={selectedUserId}
              onChange={setSelectedUserId}
              excludeIds={[owner, ...collaborators]}
              placeholder={t('share.addPlaceholder')}
            />
            <button className="ze-button" data-size="sm" type="button" onClick={add}>
              {t('share.add')}
            </button>
          </div>
          {err && <div className="ze-header__acl-error">{err}</div>}
        </>
      )}
    </div>
  )
}
