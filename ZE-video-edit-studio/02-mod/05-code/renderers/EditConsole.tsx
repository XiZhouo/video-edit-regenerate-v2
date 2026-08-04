/**
 * EditConsole — 控制台:先看清"这次会发什么",再(可选)补一句话,然后发。
 *
 * ## 范围是从标注算出来的,不是让人另外框一次
 *
 * 对齐草图设计 v1:那边 `region-bbox.ts::computeGenerationRegion` 把所有可见标注的
 * bbox 并起来 + padding,人从不选区域;提示词也是 `use-prompt-generator.ts` 从标注
 * 自动生成、灌进输入框,人改了才停止自动跟随(`PromptEditor.tsx:143` 的 manuallyEdited)。
 *
 * 视频多一根时间轴,所以标注同时推导出**空间**(它自己的 bbox,烧成那个彩框)和
 * **时间焦点**。原视频符合模型时长限制时整段提交；只有过长时才按焦点截取，
 * 并补成模型区间内的整秒长度。
 *
 * 时间尺上拖出的那一段是**可选覆盖**:拖了就只发那一段,不拖就按标注算的发。
 *
 * 视觉全部贴 build-v3 原子(.ze-stack/.ze-row/.ze-text/.ze-textarea/.ze-button)。
 */
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import {
  CREDENTIALS_CHANGED_EVENT,
  requestOpenCredentials,
  useAuth,
  useT,
  type CustomPaneProps,
} from '@ze/core/04-user-facing'
import {
  apiListCredentials,
  apiListCursorModels,
  type CursorModelOption,
} from '@ze/core/04-user-facing/03-authentication'
import CanvasCard from './CanvasCard'
import ExportCard from './ExportCard'
import {
  GENERATION_MODELS,
  hasStrategyChoice,
  modelOption,
  selectionForModel,
  strategiesForModel,
} from './generationOptions'
import { generationJobPresentation } from './generationJobStatus'
import {
  modelForOptimizer,
  OPTIMIZER_PROVIDERS,
  readOptimizerPrefs,
  saveOptimizerSelection,
  type OptimizerProvider,
} from './optimizerPrefs'
import { submitPromptOptimizationRequest } from './promptOptimizationRequest'
import { exposedWindows, fmt, getStudio, subscribeStudio } from './studioStore'

type Dict = Record<string, any>
type Group = { start: number; end: number; numbers: number[]; full?: boolean }

const num = (v: any, d = 0): number => (Number.isFinite(Number(v)) ? Number(v) : d)
const round2 = (v: number) => Math.round(v * 100) / 100
const MIN_SPAN = 0.5   // 每段最短长度(与域层 send_groups.min_span 同口径)
/** 这些标注该分几次发 —— 与后端 layer_compose.send_groups 同一条规则。
 *  每条区间各自加余量,加完重叠或挨着的才并成一组。 */
function sendGroups(annotations: Dict[], padding: number, total: number): Group[] {
  const padded = annotations
    .map((a) => {
      const s = num(a.data?.start)
      const e = s + num(a.data?.duration)
      const cap = total > 0 ? total : e + padding
      return { start: Math.max(0, s - padding), end: Math.min(cap, e + padding),
               n: num(a.data?.number) }
    })
    .sort((x, y) => x.start - y.start)
  const out: Group[] = []
  for (const p of padded) {
    const last = out[out.length - 1]
    if (last && p.start <= last.end + 1e-6) {
      last.end = Math.max(last.end, p.end)
      last.numbers.push(p.n)
    } else {
      out.push({ start: p.start, end: p.end, numbers: [p.n] })
    }
  }
  // 每段撑到至少 MIN_SPAN(与域层 send_groups 的 min_span 同口径)——
  // 太短的段发不出去:clip.duration 的 schema 下限是 0.2。
  return out.map((g) => {
    let { start, end } = g
    if (end - start < MIN_SPAN) {
      const mid = (start + end) / 2
      start = Math.max(0, mid - MIN_SPAN / 2)
      end = start + MIN_SPAN
      if (total > 0 && end > total) { end = total; start = Math.max(0, total - MIN_SPAN) }
    }
    return { start: round2(start), end: round2(end),
             numbers: [...new Set(g.numbers)].sort((a, b) => a - b) }
  })
}

/** 与后端 generation_contract.plan_submission_groups 同口径。 */
function planSubmissionGroups(provider: string, total: number, focus: Group[]): Group[] {
  if (total <= 0) throw new Error('当前视频时长必须大于 0 秒')
  const minimum = provider === 'seedance' ? 4 : 1
  const maximum = provider === 'seedance' ? 15 : 10
  if (provider === 'seedance' && total < minimum - 1e-6) {
    throw new Error(`Seedance 提交视频至少需要 ${minimum} 秒，当前视频为 ${total.toFixed(2)} 秒`)
  }
  const numbers = [...new Set(focus.flatMap((group) => group.numbers))].sort((a, b) => a - b)
  if (total <= maximum + 1e-6) {
    return [{ start: 0, end: Math.round(total * 1000) / 1000, numbers, full: true }]
  }
  return focus.map((group) => {
    const start = Math.max(0, group.start)
    const end = Math.min(total, group.end)
    const alignedStart = Math.floor(start + 1e-9)
    const wholeSeconds = Math.max(minimum, Math.ceil(end - 1e-9) - alignedStart)
    if (wholeSeconds > maximum) {
      throw new Error(
        `${provider} 单次最多处理 ${maximum} 秒，当前画面引导需要覆盖 `
        + `${(end - start).toFixed(2)} 秒；请拆成多个时段`,
      )
    }
    let plannedStart = alignedStart
    let plannedEnd = plannedStart + wholeSeconds
    if (plannedEnd > total) {
      plannedEnd = total
      plannedStart = total - wholeSeconds
    }
    return {
      start: Math.round(plannedStart * 1000) / 1000,
      end: Math.round(plannedEnd * 1000) / 1000,
      numbers: group.numbers,
      full: false,
    }
  })
}

export default function EditConsole(
  { entities, entitiesByType, dispatch, statusFor, assetUrl }: CustomPaneProps,
) {
  const t = useT()
  const { user } = useAuth()
  const studio = useSyncExternalStore(subscribeStudio, getStudio, getStudio)
  const [extra, setExtra] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [failed, setFailed] = useState(false)
  const optimizerUserId = String(user?.id || '')
  const optimizerStorage = globalThis.localStorage
  const initialPrefs = useMemo(
    () => readOptimizerPrefs(optimizerStorage, optimizerUserId),
    [optimizerStorage, optimizerUserId],
  )
  const [optimizerProvider, setOptimizerProvider] = useState<OptimizerProvider>(
    initialPrefs.provider || 'codex_cli_profile',
  )
  const [optimizerModel, setOptimizerModel] = useState(
    modelForOptimizer(initialPrefs, initialPrefs.provider || 'codex_cli_profile'),
  )
  const [loadedOptimizerUserId, setLoadedOptimizerUserId] = useState(optimizerUserId)
  const [availableOptimizers, setAvailableOptimizers] = useState<OptimizerProvider[]>([])
  const [cursorModels, setCursorModels] = useState<CursorModelOption[]>([])
  const [optimizing, setOptimizing] = useState(false)
  const [optimizationId, setOptimizationId] = useState('')
  const [optimizedPreview, setOptimizedPreview] = useState('')
  const [credentialTick, setCredentialTick] = useState(0)
  const [pendingProfile, setPendingProfile] = useState<Dict | null>(null)

  const selectOptimizerProvider = (next: OptimizerProvider) => {
    const prefs = readOptimizerPrefs(optimizerStorage, optimizerUserId)
    setOptimizerProvider(next)
    setOptimizerModel(modelForOptimizer(prefs, next))
  }

  const profiles = entities as Dict[]
  const clips = (entitiesByType?.clip ?? []) as Dict[]
  const annotations = (entitiesByType?.annotation ?? []) as Dict[]
  const referenceFrames = (entitiesByType?.reference_frame ?? []) as Dict[]
  const jobs = (entitiesByType?.send_job ?? []) as Dict[]
  const optimizations = (entitiesByType?.prompt_optimization ?? []) as Dict[]
  const assets = (entitiesByType?.asset ?? []) as Dict[]
  const project = ((entitiesByType?.project ?? []) as Dict[])[0] ?? null
  const exportRows = (entitiesByType?.export ?? []) as Dict[]

  const profile = profiles[0] ?? null
  const profileData = { ...(profile?.data || {}), ...(pendingProfile || {}) }
  const rawModel = String(profileData.model || '')
  const selectedModel = modelOption(rawModel)
  const model = selectedModel.id
  const provider = selectedModel.provider
  const allowedStrategies = strategiesForModel(model)
  const storedStrategy = String(profileData.strategy
    || (num(profileData.mode, 1) === 2 ? 'modified_frames' : 'annotated_video'))
  const strategy = allowedStrategies.includes(storedStrategy as any)
    ? storedStrategy
    : allowedStrategies[0]
  const canChooseStrategy = hasStrategyChoice(model)
  const padding = profileData.paddingSec != null
    ? Math.max(0, num(profileData.paddingSec)) : 0.5

  useEffect(() => {
    if (!pendingProfile || !profile) return
    const committed = profile.data || {}
    if (Object.entries(pendingProfile).every(([key, value]) => committed[key] === value)) {
      setPendingProfile(null)
    }
  }, [pendingProfile, profile])

  const visibleMarks = useMemo(
    () => annotations.filter((a) => a.data?.visible !== false)
      .sort((a, b) => num(a.data?.number) - num(b.data?.number)),
    [annotations])
  const visibleLayers = clips.filter((c) => c.data?.visible !== false)
  const total = visibleLayers.reduce(
    (m, c) => Math.max(m, num(c.data?.start) + num(c.data?.duration)), 0)

  /** 真正露出来的标注 + 它们各自露出来的时间窗口 —— 与后端 exposed_windows 同口径。
   *  被上面的视频层盖住的框,画面上看不见,发送时不存在(「看见什么就发什么」)。 */
  const exposure = useMemo(
    () => exposedWindows(clips as any, visibleMarks as any, 0, Math.max(total, 0.001)),
    [clips, visibleMarks, total])
  const exposedMarks = useMemo(
    () => visibleMarks.filter((a) => exposure.has(a.id)), [visibleMarks, exposure])

  /** 这次要发哪几段 —— **只由标注推导**。
   *  时间尺上拖动只是移动播放头,不该、也不能影响发出去的是哪一段:
   *  从前它是「手动覆盖」,结果随手拖出的 0.17 秒把整条管线带崩了
   *  (clip.duration 的 schema 下限是 0.2,结果层在最后一步被拒)。 */
  const focusGroups = useMemo<Group[]>(() => {
    // 被遮挡打断成两截的标注,两截各自成段 —— 与后端 send_groups 读 windows 同口径
    const spans: Dict[] = []
    exposedMarks.forEach((a) => {
      (exposure.get(a.id) || []).forEach(([s0, e0]) => {
        spans.push({ id: a.id, data: { ...a.data, start: s0, duration: e0 - s0 } })
      })
    })
    return sendGroups(spans, padding, total)
  }, [exposedMarks, exposure, padding, total])
  const submissionPlan = useMemo(() => {
    try {
      return { groups: planSubmissionGroups(provider, total, focusGroups), error: '' }
    } catch (error: any) {
      return { groups: [] as Group[], error: String(error?.message || error) }
    }
  }, [focusGroups, provider, total])
  const groups = submissionPlan.groups

  /** 提示词从标注自动生成(v1 的 usePromptGenerator);人一改就不再自动覆盖。 */
  const autoPrompt = useMemo(
    () => exposedMarks.filter((a) => String(a.data?.label || '').trim())
      .map((a) => `${num(a.data?.number)}. ${a.data?.label}`).join('\n'),
    [exposedMarks])
  const [edited, setEdited] = useState(false)
  const lastAuto = useRef('')
  useEffect(() => {
    if (!edited && autoPrompt !== lastAuto.current) {
      lastAuto.current = autoPrompt
      setExtra('')                 // 自动那部分由后端拼,这里的框只放"补一句全局的话"
    }
  }, [autoPrompt, edited])

  const recent = useMemo(() => [...jobs].slice(-4).reverse(), [jobs])
  const activeJobCount = jobs.filter((job) => generationJobPresentation(
    job,
    statusFor?.('send_job', job.id),
  ).active).length
  const enabledReferences = useMemo(
    () => referenceFrames.filter((cue) => cue.data?.enabled !== false)
      .sort((a, b) => num(a.data?.at) - num(b.data?.at)),
    [referenceFrames])
  const groupsWithoutReference = strategy === 'modified_frames'
    ? groups.filter((group) => !enabledReferences.some(
      (cue) => num(cue.data?.at) >= group.start && num(cue.data?.at) <= group.end))
    : []

  useEffect(() => {
    if (loadedOptimizerUserId === optimizerUserId) return
    const prefs = readOptimizerPrefs(optimizerStorage, optimizerUserId)
    const nextProvider = prefs.provider || 'codex_cli_profile'
    setOptimizerProvider(nextProvider)
    setOptimizerModel(modelForOptimizer(prefs, nextProvider))
    setLoadedOptimizerUserId(optimizerUserId)
  }, [loadedOptimizerUserId, optimizerStorage, optimizerUserId])

  useEffect(() => {
    const refresh = () => setCredentialTick((value) => value + 1)
    const onVisibility = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    window.addEventListener('focus', refresh)
    window.addEventListener(CREDENTIALS_CHANGED_EVENT, refresh)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.removeEventListener('focus', refresh)
      window.removeEventListener(CREDENTIALS_CHANGED_EVENT, refresh)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    apiListCredentials().then(({ credentials }) => {
      if (cancelled) return
      const active = credentials
        .filter((row) => row.status === 'active' && OPTIMIZER_PROVIDERS.includes(
          row.provider as OptimizerProvider))
      const providers = [...new Set(active.map((row) => row.provider as OptimizerProvider))]
      setAvailableOptimizers(providers)
      const preferred = readOptimizerPrefs(optimizerStorage, optimizerUserId).provider
        || (active.find((row) => row.metadata?.is_default)?.provider as OptimizerProvider | undefined)
      const next = preferred && providers.includes(preferred)
        ? preferred
        : providers.includes(optimizerProvider)
          ? optimizerProvider
          : providers[0] || optimizerProvider
      if (next !== optimizerProvider) selectOptimizerProvider(next)
    }).catch(() => {
      if (!cancelled) setAvailableOptimizers([])
    })
    return () => { cancelled = true }
  }, [credentialTick, optimizerUserId])

  useEffect(() => {
    if (loadedOptimizerUserId !== optimizerUserId) return
    saveOptimizerSelection(
      optimizerStorage,
      optimizerUserId,
      optimizerProvider,
      optimizerModel,
    )
  }, [
    loadedOptimizerUserId,
    optimizerModel,
    optimizerProvider,
    optimizerStorage,
    optimizerUserId,
  ])

  useEffect(() => {
    if (optimizerProvider !== 'cursor_api_key'
        || !availableOptimizers.includes('cursor_api_key')) {
      setCursorModels([])
      return
    }
    let cancelled = false
    apiListCursorModels().then((models) => {
      if (cancelled) return
      setCursorModels(models)
      setOptimizerModel((current) => {
        const preferred = modelForOptimizer(
          readOptimizerPrefs(optimizerStorage, optimizerUserId),
          'cursor_api_key',
        ) || current
        if (preferred && models.some((item) => item.id === preferred)) return preferred
        return models[0]?.id || ''
      })
    }).catch((err: any) => {
      if (cancelled) return
      setCursorModels([])
      setOptimizerModel('')
      setFailed(true)
      setNote(String(err?.message || err))
    })
    return () => { cancelled = true }
  }, [optimizerProvider, availableOptimizers, credentialTick, optimizerUserId])

  useEffect(() => {
    if (!optimizationId) return
    const current = optimizations.find((item) => item.id === optimizationId)
    const status = String(current?.data?.status || '')
    if (status === 'succeeded') {
      setOptimizing(false)
      setOptimizedPreview(String(current?.data?.optimizedPrompt || ''))
      setNote(t('mod.console.optimizeReady'))
      setFailed(false)
    } else if (status === 'failed' || status === 'cancelled') {
      setOptimizing(false)
      setFailed(true)
      setNote(String(current?.data?.lastError || t('mod.console.optimizeFailed')))
    }
  }, [optimizationId, optimizations, t])

  /** 唯一的门:有没有至少一条显示中的标注,且它写了要改成什么。 */
  const blockReason = !visibleMarks.length ? t('mod.console.needMark')
    : !exposedMarks.length ? t('mod.console.allCovered')
    : !autoPrompt && !extra.trim() ? t('mod.console.needLabel')
    : !visibleLayers.length ? t('mod.console.needLayer')
    : submissionPlan.error ? submissionPlan.error
    : !groups.length ? t('mod.console.needSpan')
    : strategy === 'modified_frames' && !enabledReferences.length
      ? t('mod.console.needReference')
    : groupsWithoutReference.length ? t('mod.console.referenceEverySpan')
    : ''
  const canSend = !blockReason && !busy

  const send = async () => {
    setBusy(true); setNote(''); setFailed(false)
    try {
      await dispatch('edit.send', { prompt: extra.trim(), provider, strategy, model })
      setNote(t('mod.console.sentN', { n: groups.length }))
    } catch (err: any) {
      setFailed(true)
      setNote(String(err?.message || err || t('mod.console.failed')))
    } finally {
      setBusy(false)
    }
  }

  const optimizePrompt = async () => {
    if (!availableOptimizers.includes(optimizerProvider)) {
      requestOpenCredentials({ page: 'connect', provider: optimizerProvider, capability: 'llm' })
      setCredentialTick((value) => value + 1)
      return
    }
    if (optimizerProvider === 'cursor_api_key' && !optimizerModel) {
      setFailed(true); setNote(t('mod.console.optimizerModelRequired')); return
    }
    const requestId = `opt-${globalThis.crypto?.randomUUID?.() || Date.now().toString(36)}`
    setOptimizing(true); setOptimizationId(requestId); setOptimizedPreview('')
    setFailed(false); setNote(t('mod.console.optimizing'))
    try {
      const accepted = await submitPromptOptimizationRequest(dispatch, {
        requestId,
        prompt: extra.trim(),
        optimizerProvider,
        optimizerModel,
        provider,
        strategy,
        model,
      })
      if (!accepted) {
        setOptimizationId('')
        setOptimizing(false)
        setFailed(true)
        setNote(t('mod.console.optimizeFailed'))
      }
    } catch (err: any) {
      setOptimizationId(''); setOptimizing(false); setFailed(true)
      setNote(String(err?.message || err || t('mod.console.optimizeFailed')))
    }
  }

  const Row = ({ k, children }: { k: string; children: React.ReactNode }) => (
    <div className="ze-row" data-gap="2" style={{ alignItems: 'baseline' }}>
      <span className="ze-text" data-role="tertiary" style={{ width: 64, flexShrink: 0 }}>{k}</span>
      <span className="ze-text ze-fill" data-role="secondary">{children}</span>
    </div>
  )

  const updateProfile = async (patches: Dict) => {
    setPendingProfile((current) => ({ ...(current || {}), ...patches }))
    try {
      await dispatch('send_profile.set', patches)
      setNote('')
    } catch (err: any) {
      setPendingProfile(null)
      setFailed(true)
      setNote(String(err?.message || err))
    }
  }

  return (
    <div className="ze-stack ze-scroll ze-pad" data-gap="4" data-p="3" style={{ height: '100%' }}>

      {/* 画布规格 —— 标注坐标系 = 发送规格 = 导出规格,三者同一个数 */}
      <CanvasCard project={project} assets={assets} annotations={annotations}
                  dispatch={dispatch} />

      <section className="ze-stack" data-gap="1">
        <span className="ze-text" data-role="title">{t('mod.console.generation')}</span>
        <label className="ze-row" data-gap="2">
          <span className="ze-text" data-role="tertiary">{t('mod.console.model')}</span>
          <select className="ze-select ze-fill" value={model}
                  onChange={(event) => void updateProfile(
                    selectionForModel(event.target.value, strategy))}>
            {GENERATION_MODELS.map((option) => (
              <option key={option.id} value={option.id}>{option.label}</option>
            ))}
          </select>
        </label>
        {canChooseStrategy && (
          <label className="ze-row" data-gap="2">
            <span className="ze-text" data-role="tertiary">{t('mod.console.strategy')}</span>
            <select className="ze-select ze-fill" value={strategy}
                    onChange={(event) => void updateProfile({
                      strategy: event.target.value,
                      mode: event.target.value === 'modified_frames' ? 2 : 1,
                    })}>
              {allowedStrategies.map((value) => (
                <option key={value} value={value}>{t(`mod.strategy.${value}`)}</option>
              ))}
            </select>
          </label>
        )}
        <span className="ze-text" data-role="tertiary">
          {t(`mod.strategyHelp.${strategy}`)}
        </span>
        {strategy === 'modified_frames' && (
          <div className="ze-stack" data-gap="1">
            <button type="button" className="ze-button" data-size="sm"
                    onClick={() => void dispatch('reference_frame.add', {
                      at: Number(studio.playhead.toFixed(3)),
                    })}>
              {t('mod.console.addReferenceAt', { at: fmt(studio.playhead) })}
            </button>
            <span className="ze-text" data-role="tertiary">
              {enabledReferences.length
                ? t('mod.console.referenceList', {
                    times: enabledReferences.map((cue) => fmt(num(cue.data?.at)))
                      .join(t('mod.console.markSep')),
                  })
                : t('mod.console.noReference')}
            </span>
          </div>
        )}
      </section>

      {/* 这次会发什么 —— 按下去之前要能核对的东西,而且要说清是怎么算出来的 */}
      <section className="ze-stack" data-gap="1">
        <span className="ze-text" data-role="title">{t('mod.console.whatWillBeSent')}</span>
        <Row k={t('mod.console.span')}>
          {groups.length === 0
            ? t('mod.console.noSpan')
            : (
              <span className="ze-stack" data-gap="0-5" style={{ display: 'inline-flex' }}>
                {groups.map((g, i) => (
                  <span key={i} className="ze-text" data-role="secondary" data-mono="true">
                    {fmt(g.start)} – {fmt(g.end)}
                    {g.numbers.length > 0 && (
                      <span className="ze-text" data-role="tertiary" data-mono="false">
                        {' '}{t('mod.console.fromMarks', { marks: g.numbers.join(t('mod.console.markSep')) })}
                      </span>
                    )}
                  </span>
                ))}
              </span>
            )}
        </Row>
        <Row k={t('mod.console.howDerived')}>
          {groups.some((group) => group.full)
            ? t('mod.console.derivedFullVideo', { duration: total.toFixed(2) })
            : t('mod.console.derivedClippedVideo')}
        </Row>
        <Row k={t('mod.console.layers')}>
          {t('mod.console.visibleOf', { visible: visibleLayers.length, total: clips.length })}
        </Row>
        <Row k={t('mod.console.marks')}>
          {autoPrompt || t('mod.console.noMarks')}
        </Row>
        <Row k={t('mod.console.model')}>{selectedModel.label}</Row>
        <Row k={t('mod.console.strategy')}>{t(`mod.strategy.${strategy}`)}</Row>
      </section>

      {/* 每条标注自己带着"这块要改成什么";这个框只补一句全局的话 */}
      <section className="ze-stack" data-gap="1">
        <span className="ze-text" data-role="title">{t('mod.console.globalNote')}</span>
        <textarea className="ze-textarea" rows={3} value={extra}
                  onChange={(e) => { setEdited(true); setExtra(e.target.value) }}
                  placeholder={t('mod.console.globalNotePlaceholder')} />
        <div className="ze-row" data-gap="2">
          <select className="ze-select ze-fill" value={optimizerProvider}
                  aria-label={t('mod.console.optimizerAccount')}
                  onChange={(event) => selectOptimizerProvider(
                    event.target.value as OptimizerProvider)}>
            {OPTIMIZER_PROVIDERS.map((value) => (
              <option key={value} value={value}>
                {t(`mod.optimizerProvider.${value}`)}
                {availableOptimizers.includes(value) ? '' : ` · ${t('mod.console.notConnected')}`}
              </option>
            ))}
          </select>
          {optimizerProvider === 'cursor_api_key' ? (
            <select className="ze-select ze-fill" value={optimizerModel}
                    aria-label={t('mod.console.optimizerModel')}
                    onChange={(event) => setOptimizerModel(event.target.value)}>
              {!cursorModels.length && <option value="">{t('mod.console.noCursorModels')}</option>}
              {cursorModels.map((option) => (
                <option key={option.id} value={option.id}>{option.display_name || option.id}</option>
              ))}
            </select>
          ) : (
            <input className="ze-input ze-fill" value={optimizerModel}
                   aria-label={t('mod.console.optimizerModel')}
                   placeholder={t('mod.console.accountDefaultModel')}
                   onChange={(event) => setOptimizerModel(event.target.value)} />
          )}
        </div>
        <button type="button" className="ze-button" disabled={optimizing}
                onClick={() => void optimizePrompt()}>
          {optimizing ? t('mod.console.optimizing') : t('mod.console.optimize')}
        </button>
        {optimizedPreview && (
          <div className="ze-stack" data-gap="1">
            <span className="ze-text" data-role="secondary">{t('mod.console.optimizePreview')}</span>
            <textarea className="ze-textarea" rows={6} readOnly value={optimizedPreview} />
            <div className="ze-row" data-gap="2">
              <button type="button" className="ze-button" data-variant="primary"
                      onClick={() => {
                        setEdited(true); setExtra(optimizedPreview); setOptimizedPreview('')
                        setNote(t('mod.console.optimizeAdopted'))
                      }}>
                {t('mod.console.optimizeAdopt')}
              </button>
              <button type="button" className="ze-button"
                      onClick={() => setOptimizedPreview('')}>
                {t('mod.console.optimizeDiscard')}
              </button>
            </div>
          </div>
        )}
      </section>

      <button type="button" className="ze-button" data-variant="primary"
              disabled={!canSend} onClick={() => void send()}>
        {busy ? t('mod.console.sending')
              : groups.length > 1 ? t('mod.console.sendN', { n: groups.length })
              : t('mod.console.send')}
      </button>

      {blockReason && (
        <span className="ze-text" data-role="tertiary">{blockReason}</span>
      )}

      {note && (
        <span className="ze-text" data-role="tertiary" data-tone={failed ? 'danger' : undefined}
              style={{ whiteSpace: 'pre-wrap' }}>
          {note}
        </span>
      )}

      <span className="ze-text" data-role="tertiary">{t('mod.console.explainLayer')}</span>

      {/* 导出:精修完拿不出去等于没修 */}
      <ExportCard clips={clips} exports={exportRows} project={project}
                  dispatch={dispatch} statusFor={statusFor} assetUrl={assetUrl} />

      {/* 最近几次:任务在跑还是回来了 */}
      {recent.length > 0 && (
        <section className="ze-stack" data-gap="1">
          <div className="ze-row" data-gap="2">
            <span className="ze-text ze-fill" data-role="title">
              {t('mod.console.recent')}
            </span>
            {activeJobCount > 0 && (
              <span className="ze-text" data-role="tertiary">
                {t('mod.console.activeJobs', { n: activeJobCount })}
              </span>
            )}
          </div>
          {recent.map((j) => {
            const st = statusFor?.('send_job', j.id)
            const presentation = generationJobPresentation(j, st)
            return (
              <div key={j.id} className="ze-stack" data-gap="0">
                <div className="ze-row" data-gap="2">
                  <span className="ze-text ze-fill" data-role="tertiary"
                        data-truncate="true">
                    {j.name}
                  </span>
                  <span className="ze-text" data-role="tertiary"
                        data-tone={presentation.state === 'failed'
                          || presentation.state === 'runtimeMissing' ? 'danger' : undefined}>
                    {t(`mod.console.jobStatus.${presentation.state}`)}
                  </span>
                </div>
                {presentation.detail && (
                  <span className="ze-text" data-role="tertiary" data-tone="danger"
                        style={{ whiteSpace: 'pre-wrap' }}>
                    {presentation.detail}
                  </span>
                )}
              </div>
            )
          })}
        </section>
      )}
    </div>
  )
}
