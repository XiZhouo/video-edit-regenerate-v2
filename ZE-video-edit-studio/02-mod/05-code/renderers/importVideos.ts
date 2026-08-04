/**
 * importVideos — 把视频文件变成时间线上的一层。
 *
 * 从前「导入素材」只建 asset,不建 clip —— 空项目导入之后时间线还是空的,
 * 而空态文案让人「先导入一段素材」,人照做了却什么都没发生。
 *
 * 一次导入两步,两步都得有:
 *   ① `asset.import` —— 落素材(后端顺手 ffprobe 探真实规格;空项目还会据此定画布)
 *   ② `entity.add`   —— 建图层:**接在现有内容末尾,放到最上面一层**(规划 §3.1)
 *
 * 第二步的 assetId 从第一步的返回体里取:dispatch 返回整个 Operation,
 * 新建实体在 patches 里以 `op:'create'` 出现,`after` 就是那个实体。
 *
 * 画布与时间线两个面板都用它 —— 拖到哪儿都能导入,行为必须一模一样。
 */

type Dict = Record<string, any>
type Dispatch = (cmd: string, payload?: Record<string, unknown>) => Promise<unknown> | unknown
type Upload = (bytes: Uint8Array, mime: string, ext: string) => Promise<Dict>

const num = (v: any, d = 0): number => (Number.isFinite(Number(v)) ? Number(v) : d)

/** 默认时长:素材探不出长度时给它一个能看见的段,而不是 0(0 长度的层等于没导入)。 */
const FALLBACK_DURATION = 4

/** 从 dispatch 的返回体里捞出这条命令新建的实体。 */
function createdEntity(result: unknown, type: string): Dict | null {
  const patches = (result as Dict)?.op?.patches
  if (!Array.isArray(patches)) return null
  for (const p of patches) {
    if (p?.op === 'create' && p?.entity_type === type && p?.after) return p.after as Dict
  }
  return null
}

export function isVideoFile(f: File): boolean {
  return f.type.startsWith('video/') || /\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(f.name)
}

export function isImageFile(f: File): boolean {
  return f.type.startsWith('image/') || /\.(png|jpe?g|webp|gif|bmp|avif)$/i.test(f.name)
}

async function videoSpec(file: File): Promise<{ duration: number; width: number; height: number }> {
  return await new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const video = document.createElement('video')
    const done = (value: { duration: number; width: number; height: number }) => {
      URL.revokeObjectURL(url)
      resolve(value)
    }
    video.preload = 'metadata'
    video.onloadedmetadata = () => done({
      duration: Number.isFinite(video.duration) ? video.duration : 0,
      width: video.videoWidth || 0,
      height: video.videoHeight || 0,
    })
    video.onerror = () => done({ duration: 0, width: 0, height: 0 })
    video.src = url
  })
}

async function imageSpec(file: File): Promise<{ width: number; height: number }> {
  return await new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const image = new Image()
    const done = (value: { width: number; height: number }) => {
      URL.revokeObjectURL(url)
      resolve(value)
    }
    image.onload = () => done({ width: image.naturalWidth || 0, height: image.naturalHeight || 0 })
    image.onerror = () => done({ width: 0, height: 0 })
    image.src = url
  })
}

/** 时间线上现有内容的末尾 / 最上面那一层 —— 新素材接在它们后面、上面。 */
function tailOf(clips: Dict[]): { end: number; topLayer: number } {
  let end = 0, topLayer = -1
  for (const c of clips) {
    end = Math.max(end, num(c.data?.start) + num(c.data?.duration))
    topLayer = Math.max(topLayer, num(c.data?.layer))
  }
  return { end, topLayer }
}

/**
 * 导入若干视频文件,每个都变成一层。返回这次真正建成的层数。
 * 出错就抛 —— 调用方负责把话说给人听,不许静默吞掉。
 */
export async function importVideos(files: File[], ctx: {
  clips: Dict[]
  dispatch: Dispatch
  uploadAsset?: Upload
}): Promise<number> {
  if (!ctx.uploadAsset) throw new Error('uploadAsset unavailable')
  const usable = files.filter(isVideoFile)
  let { end, topLayer } = tailOf(ctx.clips)
  let made = 0

  for (const file of usable) {
    const spec = await videoSpec(file)
    const ext = file.name.includes('.') ? `.${file.name.split('.').pop()}` : '.mp4'
    const ref = await ctx.uploadAsset(new Uint8Array(await file.arrayBuffer()),
                                      file.type || 'video/mp4', ext)
    const name = file.name.replace(/\.[^.]+$/, '')
    const res = await ctx.dispatch('asset.import',
                                   { name, assetType: 'video', file: ref,
                                     duration: spec.duration, width: spec.width,
                                     height: spec.height })
    const asset = createdEntity(res, 'asset')
    if (!asset?.id) continue                 // 素材没落成,别建一个指向空气的图层

    const duration = num(asset.data?.duration) || FALLBACK_DURATION
    topLayer += 1
    await ctx.dispatch('entity.add', {
      type: 'clip', name,
      data: { assetId: asset.id, layer: topLayer, visible: true,
              start: Number(end.toFixed(2)), duration: Number(duration.toFixed(2)),
              sourceIn: 0, xKind: 'source' },
    })
    end += duration
    made += 1
  }
  return made
}

/** 导入图片并立即成为一条画面引导；自动去背景在独立 stage 中进行。 */
export async function importGuideImages(files: File[], ctx: {
  clips: Dict[]
  dispatch: Dispatch
  uploadAsset?: Upload
  playhead: number
  duration: number
  canvasW: number
  canvasH: number
}): Promise<number> {
  if (!ctx.uploadAsset) throw new Error('uploadAsset unavailable')
  const usable = files.filter(isImageFile)
  let made = 0
  for (const file of usable) {
    const spec = await imageSpec(file)
    const ext = file.name.includes('.') ? `.${file.name.split('.').pop()}` : '.png'
    const ref = await ctx.uploadAsset(new Uint8Array(await file.arrayBuffer()),
                                      file.type || 'image/png', ext)
    const name = file.name.replace(/\.[^.]+$/, '')
    const res = await ctx.dispatch('asset.import', {
      name, assetType: 'image', file: ref, width: spec.width, height: spec.height,
    })
    const asset = createdEntity(res, 'asset')
    if (!asset?.id) continue

    const maxW = ctx.canvasW * 0.38
    const maxH = ctx.canvasH * 0.38
    const ratio = spec.width > 0 && spec.height > 0 ? spec.width / spec.height : 1
    let width = maxW, height = width / ratio
    if (height > maxH) { height = maxH; width = height * ratio }
    await ctx.dispatch('annotation.add', {
      kind: 'image',
      label: name,
      refImageAssetId: asset.id,
      bboxX: Math.round((ctx.canvasW - width) / 2),
      bboxY: Math.round((ctx.canvasH - height) / 2),
      bboxW: Math.round(width),
      bboxH: Math.round(height),
      start: Number(ctx.playhead.toFixed(3)),
      duration: Number(Math.max(0, ctx.duration - ctx.playhead).toFixed(3)),
    })
    // 原图立即可见；处理完成后画布会自动改用 processedRef。
    void Promise.resolve(ctx.dispatch('asset.cutout', { id: asset.id })).catch(() => undefined)
    made += 1
  }
  return made
}
