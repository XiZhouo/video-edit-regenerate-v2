/**
 * CanvasCard — 画布规格:输出画幅 + 帧率。
 *
 * 画布同时回答三件事:标注 bbox 的坐标系、发给 AI 的段是什么规格、导出的成品是什么
 * 规格。三者必须是同一个数,否则「框画在哪儿」和「改在哪儿」会脱开(规划 INV-5)。
 * 这个数从前只在文档里躺着,人改不了 —— 于是 16:9 素材想做成竖版是做不到的。
 *
 * 改画布走通用 `entity.update`(规划裁定:不新增命令)。改画布**不动任何 clip 几何**:
 * 几何存的是比例,画幅一变会自动按新画幅重新适配。
 *
 * 输入框走**失焦提交**:边打字边派发会把「19」当成宽度打回来(schema 下限 16),
 * 而且每个字符一个 Operation,undo 要按几十次。
 */
import { useEffect, useMemo, useState } from 'react'
import { useT, type CustomPaneProps } from '@ze/core/04-user-facing'

type Dict = Record<string, any>
type Preset = { id: string; w: number; h: number }

const num = (v: any, d = 0): number => (Number.isFinite(Number(v)) ? Number(v) : d)

/** 常用画幅。竖版在前面 —— 精修台的活多半来自竖屏投放。 */
const PRESETS: Preset[] = [
  { id: 'portrait', w: 1080, h: 1920 },
  { id: 'landscape', w: 1920, h: 1080 },
  { id: 'square', w: 1080, h: 1080 },
  { id: 'classic', w: 1440, h: 1080 },
]

/** 约掉最大公因数 —— 「1920×1080」读作 16:9 比读作两个数快得多。 */
function ratioOf(w: number, h: number): string {
  const a = Math.round(w), b = Math.round(h)
  if (a <= 0 || b <= 0) return ''
  const gcd = (x: number, y: number): number => (y ? gcd(y, x % y) : x)
  const g = gcd(a, b) || 1
  const rw = a / g, rh = b / g
  return rw <= 100 && rh <= 100 ? `${rw}:${rh}` : `${(a / b).toFixed(2)}:1`
}

export default function CanvasCard(
  { project, assets, annotations, dispatch }:
  { project: Dict | null; assets: Dict[]; annotations: Dict[]
    dispatch: CustomPaneProps['dispatch'] },
) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [locked, setLocked] = useState(true)
  const [draft, setDraft] = useState<{ width: string; height: string; fps: string } | null>(null)
  const [note, setNote] = useState('')

  const w = num(project?.data?.width), h = num(project?.data?.height)
  const fps = num(project?.data?.fps, 30)

  // 文档变了(别处改的、撤销的)就丢掉草稿,别让人对着一个陈旧的数编辑
  useEffect(() => { setDraft(null) }, [w, h, fps])

  const cur = draft ?? { width: String(w), height: String(h), fps: String(fps) }

  /** 首个素材 —— 「按首素材」取它的规格。空项目导入第一段时后端已经自动做了,
   *  这个按钮管的是「后来又想改回去照着素材来」。 */
  const firstAsset = useMemo(
    () => assets.find((a) => num(a.data?.width) > 0 && num(a.data?.height) > 0) || null,
    [assets])

  const apply = async (patches: Dict) => {
    if (!project) return
    setNote('')
    try {
      await dispatch('entity.update', {
        type: 'project', id: project.id, expected_version: project.version, patches,
      })
      await rescaleMarks(num(patches.width, w), num(patches.height, h))
    } catch (err: any) {
      setNote(String(err?.message || err))
    }
  }

  /** 画幅一变,标注框跟着按比例走。
   *
   *  bbox 存的是**画布像素**,而画布是可变的 —— 1280×720 改成 1920×1080(同是 16:9,
   *  只是出片分辨率高了)后,原地不动的框会缩成画面的 2/3,人看着框就跑了。
   *  换比例时严格说谁都不对(画面本身也重新适配了),但按比例走至少让框留在画面上、
   *  留在人放它的那个相对位置。
   *
   *  **只覆盖 Web 这条路**:画布也能被 AI/CLI 用通用 `entity.update` 改,那条路上
   *  框不会跟着动 —— 规划裁定不为改画布新增命令,而 builtin 命令挂不上级联钩子。
   *  真要根治得把 bbox 改存比例(与片段几何同一套规则),那是独立一项改动。 */
  const rescaleMarks = async (nw: number, nh: number) => {
    const sx = w > 0 && nw > 0 ? nw / w : 1
    const sy = h > 0 && nh > 0 ? nh / h : 1
    if (Math.abs(sx - 1) < 1e-6 && Math.abs(sy - 1) < 1e-6) return
    for (const a of annotations) {
      await dispatch('entity.update', {
        type: 'annotation', id: a.id, expected_version: a.version,
        patches: {
          bboxX: Math.round(num(a.data?.bboxX) * sx),
          bboxY: Math.round(num(a.data?.bboxY) * sy),
          bboxW: Math.round(num(a.data?.bboxW) * sx),
          bboxH: Math.round(num(a.data?.bboxH) * sy),
        },
      })
    }
  }

  /** 失焦提交:把草稿里的三个数一次写回去。锁比例时另一边已经在打字时跟着算好了。 */
  const commit = async () => {
    if (!draft || !project) return
    const nw = Math.round(num(draft.width, w)), nh = Math.round(num(draft.height, h))
    const nf = num(draft.fps, fps)
    setDraft(null)
    const patches: Dict = {}
    if (nw !== w && nw > 0) patches.width = nw
    if (nh !== h && nh > 0) patches.height = nh
    if (nf !== fps && nf > 0) patches.fps = nf
    if (Object.keys(patches).length) await apply(patches)
  }

  /** 锁比例时改一边,另一边按**改之前**的画幅比例跟着走。 */
  const edit = (field: 'width' | 'height' | 'fps', value: string) => {
    const next = { ...cur, [field]: value }
    if (locked && (field === 'width' || field === 'height') && w > 0 && h > 0) {
      const v = num(value, 0)
      if (v > 0) {
        if (field === 'width') next.height = String(Math.round(v * h / w))
        else next.width = String(Math.round(v * w / h))
      }
    }
    setDraft(next)
  }

  const isActive = (p: Preset) => Math.round(w) === p.w && Math.round(h) === p.h

  if (!project) return null

  return (
    <section className="ze-stack" data-gap="1">
      <div className="ze-row" data-gap="2" style={{ alignItems: 'baseline' }}>
        <span className="ze-text" data-role="title">{t('mod.canvasCard.title')}</span>
        <span className="ze-text ze-fill" data-role="secondary" data-mono="true">
          {w > 0 && h > 0
            ? `${Math.round(w)}×${Math.round(h)} · ${ratioOf(w, h)} · ${fps}fps`
            : t('mod.canvasCard.unset')}
        </span>
        <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                onClick={() => setOpen((v) => !v)}>
          {open ? t('mod.canvasCard.collapse') : t('mod.canvasCard.adjust')}
        </button>
      </div>

      {open && (
        <div className="ze-stack" data-gap="1">
          <div className="ze-row" data-gap="1" style={{ flexWrap: 'wrap' }}>
            {PRESETS.map((p) => (
              <button key={p.id} type="button" className="ze-button" data-size="sm"
                      data-variant={isActive(p) ? 'primary' : 'ghost'}
                      onClick={() => void apply({ width: p.w, height: p.h })}>
                {t(`mod.canvasCard.preset.${p.id}`)}
              </button>
            ))}
            <button type="button" className="ze-button" data-variant="ghost" data-size="sm"
                    disabled={!firstAsset}
                    title={firstAsset ? String(firstAsset.name || '') : t('mod.canvasCard.noAsset')}
                    onClick={() => firstAsset && void apply({
                      width: Math.round(num(firstAsset.data?.width)),
                      height: Math.round(num(firstAsset.data?.height)),
                      ...(num(firstAsset.data?.fps) > 0
                        ? { fps: num(firstAsset.data?.fps) } : {}),
                    })}>
              {t('mod.canvasCard.fromAsset')}
            </button>
          </div>

          <div className="ze-row" data-gap="2" style={{ alignItems: 'center' }}>
            <label className="ze-row" data-gap="1" style={{ alignItems: 'center' }}>
              <span className="ze-text" data-role="tertiary">{t('mod.canvasCard.width')}</span>
              <input className="ze-input" type="number" min={16} max={7680} value={cur.width}
                     style={{ width: 78 }}
                     onChange={(e) => edit('width', e.target.value)}
                     onBlur={() => void commit()}
                     onKeyDown={(e) => { if (e.key === 'Enter') void commit() }} />
            </label>
            <span className="ze-text" data-role="tertiary">×</span>
            <label className="ze-row" data-gap="1" style={{ alignItems: 'center' }}>
              <span className="ze-text" data-role="tertiary">{t('mod.canvasCard.height')}</span>
              <input className="ze-input" type="number" min={16} max={7680} value={cur.height}
                     style={{ width: 78 }}
                     onChange={(e) => edit('height', e.target.value)}
                     onBlur={() => void commit()}
                     onKeyDown={(e) => { if (e.key === 'Enter') void commit() }} />
            </label>
            <button type="button" className="ze-button" data-size="sm"
                    data-variant={locked ? 'primary' : 'ghost'}
                    title={t('mod.canvasCard.lockRatioHint')}
                    onClick={() => setLocked((v) => !v)}>
              {t('mod.canvasCard.lockRatio')}
            </button>
          </div>

          <label className="ze-row" data-gap="1" style={{ alignItems: 'center' }}>
            <span className="ze-text" data-role="tertiary">{t('mod.canvasCard.fps')}</span>
            <input className="ze-input" type="number" min={1} max={120} value={cur.fps}
                   style={{ width: 78 }}
                   onChange={(e) => edit('fps', e.target.value)}
                   onBlur={() => void commit()}
                   onKeyDown={(e) => { if (e.key === 'Enter') void commit() }} />
          </label>

          <span className="ze-text" data-role="tertiary">{t('mod.canvasCard.explain')}</span>
          {note && <span className="ze-text" data-role="tertiary" data-tone="danger">{note}</span>}
        </div>
      )}
    </section>
  )
}
