/**
 * ExportCard — 精修完拿出去。
 *
 * 精修完拿不出去等于没修。这一步把整条时间线(或当前区间)按画布规格合成、编码,
 * 落成一个可下载的成品。
 *
 * 与发送链**共用同一条域层渲染路径**,唯一的差别是导出**绝不烧标注**
 * (规划 INV-2:标注只属于 AI 面,成品里不该有框)。这句话不是注释,
 * 是 test_export_end_to_end.py 用 ffprobe 钉着的。
 */
import { useMemo, useState } from 'react'
import { useT, type CustomPaneProps } from '@ze/core/04-user-facing'

type Dict = Record<string, any>

const num = (v: any, d = 0): number => (Number.isFinite(Number(v)) ? Number(v) : d)

/** 与域层 render.EXPORT_PRESETS 同名同序。 */
const PRESETS = ['mp4_high', 'mp4_std', 'webm'] as const

export default function ExportCard(
  { clips, exports: rows, project, dispatch, statusFor, assetUrl }: {
    clips: Dict[]; exports: Dict[]; project: Dict | null
    dispatch: CustomPaneProps['dispatch']
    statusFor?: CustomPaneProps['statusFor']
    assetUrl?: CustomPaneProps['assetUrl']
  },
) {
  const t = useT()
  const [preset, setPreset] = useState<string>('mp4_high')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [failed, setFailed] = useState(false)

  const visible = clips.filter((c) => c.data?.visible !== false && num(c.data?.duration) > 0)
  const total = useMemo(
    () => visible.reduce((m, c) => Math.max(m, num(c.data?.start) + num(c.data?.duration)), 0),
    [visible])

  const canvasW = num(project?.data?.width), canvasH = num(project?.data?.height)

  /** 拦在前面的原因。后端也拦,但按钮亮着点下去才被拒是最差的一种交互。 */
  const blockReason = !visible.length ? t('mod.exportCard.needLayer')
    : canvasW <= 0 || canvasH <= 0 ? t('mod.exportCard.needCanvas')
    : total <= 0 ? t('mod.exportCard.needSpan')
    : ''

  const recent = useMemo(() => [...rows].slice(-3).reverse(), [rows])

  const run = async () => {
    setBusy(true); setNote(''); setFailed(false)
    try {
      await dispatch('export.render', { preset })
      setNote(t('mod.exportCard.queued'))
    } catch (err: any) {
      setFailed(true)
      setNote(String(err?.message || err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="ze-stack" data-gap="1">
      <span className="ze-text" data-role="title">{t('mod.exportCard.title')}</span>

      <div className="ze-row" data-gap="2" style={{ alignItems: 'baseline' }}>
        <span className="ze-text" data-role="tertiary" style={{ width: 64, flexShrink: 0 }}>
          {t('mod.exportCard.spec')}
        </span>
        <span className="ze-text ze-fill" data-role="secondary" data-mono="true">
          {canvasW > 0 ? `${Math.round(canvasW)}×${Math.round(canvasH)}` : '—'}
          {' · '}{total.toFixed(2)}s{' · '}{visible.length}
          {t('mod.exportCard.layerUnit')}
        </span>
      </div>

      <div className="ze-row" data-gap="1" style={{ flexWrap: 'wrap' }}>
        {PRESETS.map((p) => (
          <button key={p} type="button" className="ze-button" data-size="sm"
                  data-variant={preset === p ? 'primary' : 'ghost'}
                  onClick={() => setPreset(p)}>
            {t(`mod.exportCard.preset.${p}`)}
          </button>
        ))}
      </div>

      <button type="button" className="ze-button" disabled={!!blockReason || busy}
              onClick={() => void run()}>
        {busy ? t('mod.exportCard.running') : t('mod.exportCard.run')}
      </button>

      {blockReason && <span className="ze-text" data-role="tertiary">{blockReason}</span>}
      {note && (
        <span className="ze-text" data-role="tertiary" data-tone={failed ? 'danger' : undefined}
              style={{ whiteSpace: 'pre-wrap' }}>{note}</span>
      )}
      <span className="ze-text" data-role="tertiary">{t('mod.exportCard.noMarks')}</span>

      {recent.map((e) => {
        const st = statusFor?.('export', e.id)
        const ref = e.data?.fileRef
        const href = ref?.sha256 && assetUrl ? assetUrl(ref) : ''
        const phase = String(st?.stage_label || st?.phase || e.data?.status || '')
        return (
          <div key={e.id} className="ze-row" data-gap="2" style={{ alignItems: 'baseline' }}>
            <span className="ze-text ze-fill" data-role="tertiary" data-truncate="true">
              {e.name}
            </span>
            {href
              ? <a className="ze-link" href={href}
                   download={`${e.name}${e.data?.preset === 'webm' ? '.webm' : '.mp4'}`}>
                  {t('mod.exportCard.download')}
                </a>
              : <span className="ze-text" data-role="tertiary">{phase}</span>}
            {e.data?.lastError && (
              <span className="ze-text" data-role="tertiary" data-tone="danger"
                    data-truncate="true">{e.data.lastError}</span>
            )}
          </div>
        )
      })}
    </section>
  )
}
