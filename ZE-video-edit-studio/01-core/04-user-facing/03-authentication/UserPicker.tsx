import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import { apiLookupUsers, apiSearchUsers, type ZeUserCard } from './authApi'

export type UserPickerProps = {
  value: string
  onChange: (userId: string) => void
  channel?: string
  excludeIds?: string[]
  placeholder?: string
  disabled?: boolean
  allowRaw?: boolean
  size?: 'sm' | 'md'
}

const WRAP: CSSProperties = { position: 'relative', width: '100%', minWidth: 0 }
const POPOVER: CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 4px)',
  left: 0,
  right: 0,
  zIndex: 50,
  maxHeight: 240,
  overflow: 'auto',
  padding: 'var(--ze-space-1)',
}
const ROW_TEXT: CSSProperties = {
  minWidth: 0,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'flex-start',
}

function displayUser(user: ZeUserCard | null | undefined, fallback: string): string {
  return user?.name || user?.email || user?.id || fallback
}

function userSubline(user: ZeUserCard): string {
  return user.email || user.id
}

function initialOf(text: string): string {
  const s = text.trim()
  return s ? s.slice(0, 1).toUpperCase() : '?'
}

function UserAvatar({ user }: { user: ZeUserCard }): ReactNode {
  if (user.avatar_url) return <img src={user.avatar_url} alt="" className="ze-user-picker__avatar" />
  return (
    <span
      aria-hidden="true"
      className="ze-user-picker__avatar ze-text"
      data-size="xs"
      data-role="secondary"
    >
      {initialOf(displayUser(user, user.id))}
    </span>
  )
}

export function UserPicker({
  value,
  onChange,
  channel = '',
  excludeIds = [],
  placeholder,
  disabled,
  allowRaw = false,
  size = 'sm',
}: UserPickerProps): ReactNode {
  const t = useT()
  const [draft, setDraft] = useState(value)
  const [selected, setSelected] = useState<ZeUserCard | null>(null)
  const [results, setResults] = useState<ZeUserCard[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const blurTimer = useRef<number | null>(null)
  const excludeKey = excludeIds.join('|')
  const exclude = useMemo(() => new Set(excludeKey ? excludeKey.split('|') : []), [excludeKey])

  useEffect(() => {
    if (!value) {
      setSelected(null)
      setDraft('')
      return
    }
    let cancelled = false
    void apiLookupUsers([value], channel ? { channel } : undefined)
      .then((r) => {
        if (cancelled) return
        const u = r.users[0] ?? null
        setSelected(u)
        setDraft(displayUser(u, value))
      })
      .catch(() => {
        if (!cancelled) {
          setSelected(null)
          setDraft(value)
        }
      })
    return () => { cancelled = true }
  }, [value, channel])

  useEffect(() => {
    if (!open || disabled) return
    let cancelled = false
    setLoading(true)
    setError('')
    const timer = window.setTimeout(() => {
      void apiSearchUsers(draft, { channel, limit: 20 })
        .then((users) => {
          if (cancelled) return
          setResults(users.filter((u) => !exclude.has(u.id) || u.id === value))
        })
        .catch((e) => {
          if (!cancelled) {
            setError(e instanceof Error ? e.message : String(e))
            setResults([])
          }
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }, 160)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [open, draft, channel, disabled, exclude, value])

  const commitRaw = () => {
    const raw = draft.trim()
    if (allowRaw && raw !== value) onChange(raw)
    if (!allowRaw && selected) setDraft(displayUser(selected, value))
  }

  const choose = (user: ZeUserCard) => {
    if (blurTimer.current) window.clearTimeout(blurTimer.current)
    setSelected(user)
    setDraft(displayUser(user, user.id))
    setOpen(false)
    onChange(user.id)
  }

  return (
    <span className="ze-user-picker" style={WRAP}>
      <input
        className="ze-input"
        data-size={size}
        value={draft}
        disabled={disabled}
        placeholder={placeholder || (channel === 'feishu' ? t('userPicker.searchFeishu') : t('userPicker.search'))}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setDraft(e.target.value)
          setOpen(true)
          if (!e.target.value.trim()) {
            setSelected(null)
            onChange('')
          } else if (allowRaw) {
            onChange(e.target.value)
          }
        }}
        onBlur={() => {
          blurTimer.current = window.setTimeout(() => {
            commitRaw()
            setOpen(false)
          }, 120)
        }}
      />
      {open && !disabled && (
        <div className="ze-popover" data-open="true" data-elevation="floating" style={POPOVER}>
          {loading ? (
            <div className="ze-menu-item" data-disabled="true">{t('userPicker.loading')}</div>
          ) : error ? (
            <div className="ze-menu-item" data-disabled="true">{error}</div>
          ) : results.length === 0 ? (
            <div className="ze-menu-item" data-disabled="true">
              {allowRaw && draft.trim() ? t('userPicker.rawHint') : t('userPicker.empty')}
            </div>
          ) : results.map((user) => (
            <button
              key={user.id}
              type="button"
              className="ze-menu-item"
              data-active={user.id === value ? 'true' : undefined}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => choose(user)}
              style={{ width: '100%', display: 'flex', gap: 'var(--ze-space-2)', alignItems: 'center' }}
            >
              <UserAvatar user={user} />
              <span style={ROW_TEXT}>
                <span className="ze-text" data-size="sm">{displayUser(user, user.id)}</span>
                <span className="ze-text" data-size="xs" data-role="tertiary">{userSubline(user)}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </span>
  )
}
