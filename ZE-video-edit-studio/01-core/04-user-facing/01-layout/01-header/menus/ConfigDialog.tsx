/**
 * ConfigDialog — header ⚙ 设置菜单的「配置…」入口打开的富配置对话框。
 *
 * 数据走 04-ops ConfigRegistry:GET /api/config 取全量项(secret 已脱敏 ***)→ 按 kind 渲染
 * 可编辑控件 → 改动经 PUT /api/config 落 .data/config.json(仅 super-admin;只读项后端拒改)。
 *
 * 三种存储后端分清(见标题下提示):偏好(主题/语言)在上方菜单、本机 localStorage;本对话框是
 * 本部署的工具/服务配置(服务端);"系统(只读)"组是框架 env 旋钮,仅展示。
 *
 * 视觉单一真相源:文字用 `.ze-text`(data-role/size/weight/tone),键值表用 `.ze-kv-list`,
 * 控件用既有 `.ze-dialog/.ze-input/.ze-select/.ze-switch/.ze-button`。JSX 只发 className + data-*,
 * 只允许结构性内联(布局 / 运行时尺寸,如 maxWidth/flex/gap)。**禁止内联视觉**(色 / 字号 / 字重 /
 * 字体 / 边框)—— 由 css_layer_guard 规则 8 LAYER_INLINE_VISUAL 强制(见 ../../../../../DESIGN-SYSTEM.md
 * 「视觉单一真相源」)。
 */

import { useEffect, useState, type ReactNode } from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'
import {
  apiGetConfigSnapshot, apiPutConfig, newAuthIdempotencyKey, useAuth, type ConfigItem,
} from '../../../03-authentication'
import { useScrollAffordance } from '../../03-editor/lib/useScrollAffordance'

export type ConfigDialogProps = {
  open: boolean
  onClose: () => void
}

function isEditable(it: ConfigItem, canManage = true): boolean {
  return canManage && !it.readonly && it.kind !== 'constant'
}

export function ConfigDialog(props: ConfigDialogProps): ReactNode {
  const t = useT()
  const { user } = useAuth()
  const canManage = !!user?.is_super_admin
  const bodyRef = useScrollAffordance('y')
  const [items, setItems] = useState<ConfigItem[]>([])
  const [version, setVersion] = useState(0)
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (!props.open) return
    setErr(''); setNotice(''); setDraft({}); setLoading(true)
    apiGetConfigSnapshot()
      .then((snapshot) => { setItems(snapshot.config); setVersion(snapshot.version) })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoading(false))
  }, [props.open])

  if (!props.open) return null

  const valueOf = (it: ConfigItem): unknown => (it.key in draft ? draft[it.key] : it.value)
  const setVal = (key: string, v: unknown) =>
    setDraft((d: Record<string, unknown>) => ({ ...d, [key]: v }))

  const save = async () => {
    if (!canManage) return
    const updates: Record<string, unknown> = {}
    for (const it of items) {
      if (!isEditable(it, canManage) || !(it.key in draft)) continue
      const v = draft[it.key]
      if (it.secret && (v === '' || v == null || v === '***')) continue   // 密钥留空/仍是掩码 = 不改
      updates[it.key] = v
    }
    if (Object.keys(updates).length === 0) { setNotice(t('config.noChange')); return }
    setSaving(true); setErr(''); setNotice('')
    try {
      const result = await apiPutConfig(
        updates,
        newAuthIdempotencyKey('config.put'),
        version,
      )
      setItems(result.config); setVersion(result.version); setDraft({})
      const { rejected } = result
      setNotice(rejected.length ? t('config.savedPartial', { keys: rejected.map((r) => r.key).join(', ') }) : t('config.saved'))
    } catch (e) {
      const typed = e as Error & { code?: string }
      if (
        typed.code === 'config_version_conflict'
        || typed.code === 'config_durability_unknown'
      ) {
        // 陈旧 revision 或 replace 后耐久性未知时，以 GET 的 canonical 视图对账；
        // draft 保留，用户仍可审阅后再次提交，而不是界面假回滚。
        void apiGetConfigSnapshot().then((snapshot) => {
          setItems(snapshot.config)
          setVersion(snapshot.version)
        }).catch(() => undefined)
      }
      setErr(typed.message)
    } finally {
      setSaving(false)
    }
  }

  // 按 group 分组,保留声明顺序
  const order: string[] = []
  const byGroup = new Map<string, ConfigItem[]>()
  for (const it of items) {
    if (!byGroup.has(it.group)) { byGroup.set(it.group, []); order.push(it.group) }
    byGroup.get(it.group)!.push(it)
  }

  return (
    <div
      className="ze-dialog__backdrop"
      role="dialog"
      aria-label={t('config.title')}
      onClick={(e) => { if (e.target === e.currentTarget) props.onClose() }}
    >
      <div
        className="ze-dialog"
        style={{ width: '100%', maxWidth: 540, maxHeight: '82vh', display: 'flex', flexDirection: 'column' }}
      >
        <div className="ze-dialog__header">
          <div className="ze-dialog__title">{t('config.title')}</div>
        </div>

        <div ref={bodyRef} className="ze-dialog__body ze-scroll-fade-y" style={{ overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 16 }}>
          <p className="ze-text" data-role="secondary" style={{ margin: 0 }}>
            {t('config.intro')}
          </p>

          {loading ? (
            <div className="ze-text" data-role="secondary">{t('common.loading')}</div>
          ) : (
            order.map((g) => (
              <section key={g} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div className="ze-text" data-weight="semibold">{g}</div>
                {byGroup.get(g)!.map((it) => (
                  <ConfigField
                    key={it.key}
                    item={it}
                    value={valueOf(it)}
                    editable={isEditable(it, canManage)}
                    onChange={(v) => setVal(it.key, v)}
                  />
                ))}
              </section>
            ))
          )}

          {err && <div className="ze-text" data-tone="danger">{err}</div>}
        </div>

        <div className="ze-dialog__footer" style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'flex-end' }}>
          {notice && <span className="ze-text" data-role="secondary" style={{ marginRight: 'auto' }}>{notice}</span>}
          <button type="button" className="ze-button" onClick={props.onClose}>{t('common.close')}</button>
          {canManage ? (
            <button type="button" className="ze-button" data-variant="primary" onClick={save} disabled={saving}>
              {saving ? t('config.saving') : t('common.save')}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}

// ============================================================
// 单字段:按 kind 渲染控件
// ============================================================

function ConfigField(props: {
  item: ConfigItem
  value: unknown
  editable: boolean
  onChange: (v: unknown) => void
}): ReactNode {
  const t = useT()
  const { item, value, editable, onChange } = props

  // 只读(框架 / 端口 / 常量)→ kv-list 行展示
  if (!editable) {
    return (
      <div className="ze-kv-list__row" title={item.desc}>
        <span className="ze-kv-list__key">{item.label}</span>
        <span className="ze-kv-list__value">{value === null || value === '' ? '—' : String(value)}</span>
      </div>
    )
  }

  const label = (
    <div className="ze-text">
      {item.label}
      {item.desc && <span className="ze-text" data-role="tertiary" style={{ marginLeft: 6 }}>{item.desc}</span>}
    </div>
  )

  if (item.kind === 'bool') {
    return (
      <label className="ze-switch__row" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type="checkbox"
          role="switch"
          className="ze-switch"
          checked={!!value}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="ze-switch__label">{item.label}</span>
      </label>
    )
  }

  if ((item.kind === 'enum' || item.kind === 'model-ref') && item.choices) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {label}
        <div className="ze-select">
          <select
            className="ze-select__control"
            value={String(value ?? '')}
            onChange={(e) => onChange(e.target.value)}
          >
            {item.kind === 'model-ref' && <option value="">{t('config.routeDefault')}</option>}
            {item.choices.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      </div>
    )
  }

  // text / endpoint / secret / path / int / float → input
  const type = item.secret ? 'password' : (item.kind === 'int' || item.kind === 'float' ? 'number' : 'text')
  // secret 后端回显 '***'(脱敏占位),输入框不显示掩码 → 视为空,用户输入才覆盖
  const shown = item.secret && String(value) === '***' ? '' : String(value ?? '')
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {label}
      <input
        className="ze-input"
        type={type}
        value={shown}
        placeholder={item.secret ? t('config.secretPlaceholder') : ''}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  )
}
