import type { ToolId } from './canvasTools'

export type Canvas2dPreset =
  | 'ps-lite'
  | 'brush-selection'
  | 'single-range-layer'
  | 'bbox-placement'
  | 'cutout-selection'
  | 'readonly-preview'

export type Canvas2dLayerKind =
  | 'base-image'
  | 'raster'
  | 'text'
  | 'vector-shape'
  | 'mask-selection'
  | 'bbox-annotation'
  | 'guide-output'
  | 'group'

export type Canvas2dBlendMode =
  | 'normal'
  | 'multiply'
  | 'screen'
  | 'overlay'
  | 'source-over'

export type Canvas2dAssetRef = {
  sha256: string
  mime?: string
  ext?: string
  size?: number
}

export type Canvas2dLayerFilter = { name: string; amount?: number }

export type Canvas2dLayerStyle = {
  stroke?: string
  fill?: string
  color?: string
  fontSize?: number
  strokeWidth?: number
  cornerRadius?: number
}

export type Canvas2dLayer = {
  id: string
  name: string
  kind: Canvas2dLayerKind
  parentId?: string | null
  role?: string
  visible: boolean
  locked: boolean
  opacity: number
  blendMode: Canvas2dBlendMode
  x: number
  y: number
  width: number
  height: number
  rotation: number
  flipX: boolean
  flipY: boolean
  assetRef?: Canvas2dAssetRef
  maskRef?: Canvas2dAssetRef
  text?: string
  shape?: Record<string, unknown>
  bbox?: { x: number; y: number; w: number; h: number; rotation?: number }
  /** Legacy single filter. New data should prefer filters[]. */
  filter?: Canvas2dLayerFilter
  filters?: Canvas2dLayerFilter[]
  meta?: Record<string, unknown>
}

export type Canvas2dDocument = {
  width: number
  height: number
  background?: Canvas2dAssetRef | string | null
  layersField?: string
  activeLayerId?: string
  viewport?: { scale: number; tx: number; ty: number }
  layers: Canvas2dLayer[]
}

export type Canvas2dFieldMapping = {
  bgField?: string
  layersField?: string
  activeLayerField?: string
  widthField?: string
  heightField?: string
}

export type Canvas2dFeatureFlags = {
  layers: boolean
  rasterPaint: boolean
  eraser: boolean
  text: boolean
  crop: boolean
  filters: boolean
  transform: boolean
  maskSelection: boolean
  bbox: boolean
  sam: boolean
  magicWand: boolean
  export: boolean
}

export type Canvas2dViewState = {
  preset?: Canvas2dPreset
  bgField?: string
  /** layered 合成模式的数据源 = 该 entity_type 的 layer 实体(类型库 layer@1;
   *  按 data.order 排序;活动层=全局 selection[layerType])。
   *  1.27.0 起取代 inline 栈面(layersField/activeLayerField/layer*Command,零消费者删除)。 */
  layerType?: string
  /** layered 模式增层命令(缺省回退内置 entity.add;mod 可声明带业务缺省字段的命令)。 */
  layerAddCommand?: string
  /** Official writable pixel-layer field. Core stores/loads a PNG layer; consumers decide its semantics. */
  pixelLayerField?: string
  /** Official writable selection pixel-layer field. Alias of pixelLayerField for range/rough-selection use cases. */
  selectionLayerField?: string
  /** Legacy alias: historical consumers used "mask" for the same writable pixel layer. */
  maskField?: string
  /** Legacy alias: historical single-range consumers used this for the same writable pixel layer. */
  rangeMaskField?: string
  geometryField?: string
  xField?: string
  yField?: string
  wField?: string
  hField?: string
  rotationField?: string
  tools?: ToolId[]
  features?: Partial<Canvas2dFeatureFlags>
  importCommand?: string
  paintCommand?: string
  /** Official command for committing a writable pixel layer PNG. */
  pixelLayerCommand?: string
  /** Legacy alias for pixelLayerCommand. */
  maskCommand?: string
  bboxUpdateCommand?: string
  exportCommand?: string
}

export const CANVAS2D_DEFAULT_FEATURES: Canvas2dFeatureFlags = {
  layers: false,
  rasterPaint: false,
  eraser: false,
  text: false,
  crop: false,
  filters: false,
  transform: false,
  maskSelection: false,
  bbox: false,
  sam: false,
  magicWand: false,
  export: false,
}

const PRESETS: Record<Canvas2dPreset, { features: Partial<Canvas2dFeatureFlags>; tools: ToolId[] }> = {
  'ps-lite': {
    features: {
      layers: true,
      rasterPaint: true,
      eraser: true,
      text: true,
      crop: true,
      filters: true,
      transform: true,
      maskSelection: true,
      bbox: true,
      sam: true,
      magicWand: true,
      export: true,
    },
    // wand/sam-points(AI 辅助选区)从工具集移除:无 handler、需 mod 的 AI command 合同 + 模型后端,
    // 且零产品消费者(飞轮「工具驱动不预造」)—— 等真实 SAM/魔棒 skill 驱动再补;不出无作用的死钮。
    tools: ['pan', 'select', 'transform', 'brush', 'eraser', 'text', 'crop', 'filter', 'rect', 'ellipse', 'lasso', 'polygon', 'export'],
  },
  'brush-selection': {
    features: { rasterPaint: true, eraser: true, maskSelection: true },
    tools: ['brush', 'eraser'],
  },
  'single-range-layer': {
    features: { rasterPaint: true, eraser: true, maskSelection: true },
    tools: ['brush', 'eraser'],
  },
  'bbox-placement': {
    features: { bbox: true, transform: true },
    tools: ['pan', 'select', 'transform'],
  },
  'cutout-selection': {
    features: { rasterPaint: true, eraser: true, maskSelection: true, sam: true, magicWand: true, export: true },
    // wand/sam-points 同 ps-lite 理由移除(无 handler/零消费者);保留手动选区 lasso/polygon。
    tools: ['brush', 'eraser', 'rect', 'ellipse', 'lasso', 'polygon'],
  },
  'readonly-preview': {
    features: {},
    tools: [],
  },
}

function isObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

function finiteNumber(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

export function isCanvas2dAssetRef(value: unknown): value is Canvas2dAssetRef {
  return isObject(value) && typeof value.sha256 === 'string'
}

export function normalizeCanvas2dFilter(raw: unknown): Canvas2dLayerFilter | null {
  if (!isObject(raw) || typeof raw.name !== 'string' || !raw.name) return null
  return {
    name: raw.name,
    ...(finiteNumber(raw.amount) !== undefined ? { amount: finiteNumber(raw.amount) } : {}),
  }
}

export function normalizeCanvas2dFilterStack(filters: unknown, legacyFilter?: unknown): Canvas2dLayerFilter[] | undefined {
  const stack = Array.isArray(filters)
    ? filters.map(normalizeCanvas2dFilter).filter((f): f is Canvas2dLayerFilter => !!f)
    : []
  if (!stack.length) {
    const legacy = normalizeCanvas2dFilter(legacyFilter)
    if (legacy) stack.push(legacy)
  }
  return stack.length ? stack : undefined
}

const DEFAULT_LAYER_SIZE = 256

export function createCanvas2dLayer(
  kind: Canvas2dLayerKind,
  patch: Partial<Canvas2dLayer> = {},
): Canvas2dLayer {
  const id = patch.id ?? `layer_${Math.random().toString(36).slice(2, 10)}`
  const width = Math.max(1, Number(patch.width ?? patch.bbox?.w ?? DEFAULT_LAYER_SIZE))
  const height = Math.max(1, Number(patch.height ?? patch.bbox?.h ?? DEFAULT_LAYER_SIZE))
  return {
    id,
    name: patch.name ?? kind,
    kind,
    parentId: patch.parentId,
    role: patch.role,
    visible: patch.visible ?? true,
    locked: patch.locked ?? false,
    opacity: Number.isFinite(patch.opacity) ? Math.max(0, Math.min(1, Number(patch.opacity))) : 1,
    blendMode: patch.blendMode ?? 'normal',
    x: Number.isFinite(patch.x) ? Number(patch.x) : 0,
    y: Number.isFinite(patch.y) ? Number(patch.y) : 0,
    width,
    height,
    rotation: Number.isFinite(patch.rotation) ? Number(patch.rotation) : 0,
    flipX: patch.flipX ?? false,
    flipY: patch.flipY ?? false,
    assetRef: patch.assetRef,
    maskRef: patch.maskRef,
    text: patch.text,
    shape: patch.shape,
    bbox: patch.bbox,
    filter: patch.filter,
    filters: patch.filters ?? (patch.filter ? [patch.filter] : undefined),
    meta: patch.meta,
  }
}

export function normalizeCanvas2dLayer(raw: unknown, index = 0): Canvas2dLayer | null {
  if (!isObject(raw) || typeof raw.kind !== 'string') return null
  const bbox = isObject(raw.bbox)
    ? {
        x: Number(raw.bbox.x ?? 0),
        y: Number(raw.bbox.y ?? 0),
        w: Number(raw.bbox.w ?? raw.width ?? DEFAULT_LAYER_SIZE),
        h: Number(raw.bbox.h ?? raw.height ?? DEFAULT_LAYER_SIZE),
        ...(raw.bbox.rotation !== undefined ? { rotation: Number(raw.bbox.rotation) } : {}),
      }
    : undefined
  return createCanvas2dLayer(raw.kind as Canvas2dLayerKind, {
    id: typeof raw.id === 'string' ? raw.id : `layer_${index + 1}`,
    name: typeof raw.name === 'string' ? raw.name : `${raw.kind} ${index + 1}`,
    parentId: typeof raw.parentId === 'string' && raw.parentId ? raw.parentId : null,
    role: typeof raw.role === 'string' ? raw.role : undefined,
    visible: typeof raw.visible === 'boolean' ? raw.visible : true,
    locked: typeof raw.locked === 'boolean' ? raw.locked : false,
    opacity: typeof raw.opacity === 'number' ? raw.opacity : 1,
    blendMode: typeof raw.blendMode === 'string' ? raw.blendMode as Canvas2dBlendMode : 'normal',
    x: typeof raw.x === 'number' ? raw.x : bbox?.x ?? 0,
    y: typeof raw.y === 'number' ? raw.y : bbox?.y ?? 0,
    width: typeof raw.width === 'number' ? raw.width : bbox?.w,
    height: typeof raw.height === 'number' ? raw.height : bbox?.h,
    rotation: typeof raw.rotation === 'number' ? raw.rotation : bbox?.rotation ?? 0,
    flipX: typeof raw.flipX === 'boolean' ? raw.flipX : false,
    flipY: typeof raw.flipY === 'boolean' ? raw.flipY : false,
    assetRef: isCanvas2dAssetRef(raw.assetRef) ? raw.assetRef : undefined,
    maskRef: isCanvas2dAssetRef(raw.maskRef) ? raw.maskRef : undefined,
    text: typeof raw.text === 'string' ? raw.text : undefined,
    shape: isObject(raw.shape) ? raw.shape : undefined,
    bbox,
    filter: normalizeCanvas2dFilter(raw.filter) ?? undefined,
    filters: normalizeCanvas2dFilterStack(raw.filters, raw.filter),
    meta: isObject(raw.meta) ? raw.meta : undefined,
  })
}

/** 读内联栈(scene.layers 形态)。**只读兼容**:信封 legacy 折回数据的渲染用;
 *  写路径已实体化(layer 实体 + entity.update CAS),不要再造内联写。 */
export function readCanvas2dLayers(data: Record<string, unknown>, layersField = 'layers'): Canvas2dLayer[] {
  const raw = data[layersField]
  if (!Array.isArray(raw)) return []
  return raw
    .map((layer, index) => normalizeCanvas2dLayer(layer, index))
    .filter((layer): layer is Canvas2dLayer => !!layer)
}

/** layer 实体(类型库 layer@1,画布主形态)→ Canvas2dLayer:data 本就 camelCase 1:1 镜像,
 *  id/name 走信封字段;order 不进 Canvas2dLayer(排序由 readLayerEntities 完成)。 */
export function layerEntityToCanvas2dLayer(e: {
  id: string; name: string; data: Record<string, unknown>
}): Canvas2dLayer | null {
  return normalizeCanvas2dLayer({ ...e.data, id: e.id, name: e.name }, 0)
}

/** layer 实体列表 → 按 parentId/order DFS 拍平的 Canvas2dLayer 栈;子层继承父层 visible/locked。 */
export function readLayerEntities(entities: ReadonlyArray<{
  id: string; name: string; data: Record<string, unknown>
}>): Canvas2dLayer[] {
  const ids = new Set(entities.map((e) => e.id))
  const parentOf = (e: { data: Record<string, unknown> }) => {
    const raw = e.data.parentId
    return typeof raw === 'string' && raw && ids.has(raw) ? raw : ''
  }
  const orderOf = (e: { id: string; data: Record<string, unknown> }) =>
    typeof e.data.order === 'number' ? (e.data.order as number) : Number.POSITIVE_INFINITY
  const sortLayerEntities = (a: { id: string; name: string; data: Record<string, unknown> }, b: { id: string; name: string; data: Record<string, unknown> }) => {
    const ao = orderOf(a)
    const bo = orderOf(b)
    if (ao !== bo) return ao - bo
    const an = a.name ?? ''
    const bn = b.name ?? ''
    if (an !== bn) return an < bn ? -1 : 1
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
  }
  const buckets = new Map<string, typeof entities[number][]>()
  for (const e of entities) {
    const key = parentOf(e)
    const arr = buckets.get(key)
    if (arr) arr.push(e)
    else buckets.set(key, [e])
  }
  for (const [key, list] of buckets) buckets.set(key, [...list].sort(sortLayerEntities))
  const out: Canvas2dLayer[] = []
  const seen = new Set<string>()
  const walk = (parentKey: string, inheritedVisible: boolean, inheritedLocked: boolean) => {
    for (const e of buckets.get(parentKey) ?? []) {
      if (seen.has(e.id)) continue
      seen.add(e.id)
      const layer = layerEntityToCanvas2dLayer(e)
      if (!layer) continue
      const visible = inheritedVisible && layer.visible
      const locked = inheritedLocked || layer.locked
      out.push({ ...layer, visible, locked })
      walk(e.id, visible, locked)
    }
  }
  walk('', true, false)
  return out
}

export function canvas2dLayerCanEdit(layer: Canvas2dLayer | undefined | null): layer is Canvas2dLayer {
  return !!layer && layer.visible && !layer.locked && layer.kind !== 'base-image' && layer.kind !== 'guide-output'
}

export function addCanvas2dLayer(layers: Canvas2dLayer[], layer: Canvas2dLayer): Canvas2dLayer[] {
  return [...layers, layer]
}

export function patchCanvas2dLayer(
  layers: Canvas2dLayer[],
  layerId: string,
  patch: Partial<Canvas2dLayer>,
): Canvas2dLayer[] {
  return layers.map((layer) => {
    if (layer.id !== layerId) return layer
    if (!canvas2dLayerCanEdit(layer)) return layer
    return createCanvas2dLayer(layer.kind, { ...layer, ...patch, id: layer.id })
  })
}

export function canvas2dLayerFilterPatch(name?: string, amount?: number): Partial<Canvas2dLayer> {
  const filter = name ? { name, ...(amount !== undefined ? { amount } : {}) } : undefined
  return { filter, filters: filter ? [filter] : undefined }
}

export function canvas2dLayerFilterCss(layer: Canvas2dLayer): string | undefined {
  const stack = layer.filters && layer.filters.length ? layer.filters : layer.filter ? [layer.filter] : []
  if (!stack.length) return undefined
  const parts: string[] = []
  for (const filter of stack) {
    const amount = filter.amount ?? 1
    if (filter.name === 'grayscale') parts.push(`grayscale(${Math.round(Math.max(0, amount) * 100)}%)`)
    else if (filter.name === 'invert') parts.push(`invert(${Math.round(Math.max(0, amount) * 100)}%)`)
    else if (filter.name === 'blur') parts.push(`blur(${Math.max(0, amount) * 8}px)`)
    else if (filter.name === 'contrast') parts.push(`contrast(${Math.max(0.1, amount) * 100}%)`)
    else if (filter.name === 'brightness') parts.push(`brightness(${Math.max(0, amount) * 100}%)`)
    else if (filter.name === 'saturation' || filter.name === 'saturate') parts.push(`saturate(${Math.max(0, amount) * 100}%)`)
    else if (filter.name === 'hue' || filter.name === 'hue-rotate') parts.push(`hue-rotate(${amount}deg)`)
  }
  return parts.length ? parts.join(' ') : undefined
}

export function canvas2dLayerStyle(layer: Canvas2dLayer): Canvas2dLayerStyle {
  const style = isObject(layer.meta?.style) ? layer.meta.style : {}
  const out: Canvas2dLayerStyle = {}
  for (const key of ['stroke', 'fill', 'color'] as const) {
    const value = style[key]
    if (typeof value === 'string' && value) out[key] = value
  }
  for (const key of ['fontSize', 'strokeWidth', 'cornerRadius'] as const) {
    const value = finiteNumber(style[key])
    if (value !== undefined) out[key] = value
  }
  return out
}

export function removeCanvas2dLayer(layers: Canvas2dLayer[], layerId: string): Canvas2dLayer[] {
  return layers.filter((layer) => layer.id !== layerId || layer.locked || layer.kind === 'base-image')
}

export function reorderCanvas2dLayer(layers: Canvas2dLayer[], layerId: string, direction: -1 | 1): Canvas2dLayer[] {
  const index = layers.findIndex((layer) => layer.id === layerId)
  const next = index + direction
  if (index < 0 || next < 0 || next >= layers.length) return layers
  const copy = [...layers]
  const [layer] = copy.splice(index, 1)
  copy.splice(next, 0, layer)
  return copy
}

export function expandCanvas2dDocument(
  data: Record<string, unknown>,
  mapping: Canvas2dFieldMapping = {},
): Canvas2dDocument {
  const layers = readCanvas2dLayers(data, mapping.layersField)
  const widthField = mapping.widthField ?? 'width'
  const heightField = mapping.heightField ?? 'height'
  const activeField = mapping.activeLayerField ?? 'activeLayerId'
  const active = typeof data[activeField] === 'string'
    ? data[activeField] as string
    : layers.find(canvas2dLayerCanEdit)?.id ?? layers[0]?.id
  return {
    width: typeof data[widthField] === 'number' ? Number(data[widthField]) : 0,
    height: typeof data[heightField] === 'number' ? Number(data[heightField]) : 0,
    background: mapping.bgField && isCanvas2dAssetRef(data[mapping.bgField]) ? data[mapping.bgField] as Canvas2dAssetRef : null,
    layersField: mapping.layersField ?? 'layers',
    activeLayerId: active,
    layers,
  }
}

export function shouldMarkRangeStale(args: {
  oldWidth?: number | null
  oldHeight?: number | null
  newWidth?: number | null
  newHeight?: number | null
  hasRangeMask?: boolean
}): boolean {
  if (!args.hasRangeMask) return false
  if (!args.oldWidth || !args.oldHeight || !args.newWidth || !args.newHeight) return false
  return args.oldWidth !== args.newWidth || args.oldHeight !== args.newHeight
}

export function isCanvas2dPreset(value: unknown): value is Canvas2dPreset {
  return typeof value === 'string' && value in PRESETS
}

export function resolveCanvas2dPreset(view: {
  preset?: Canvas2dPreset | string
  tools?: ToolId[]
  features?: Partial<Canvas2dFeatureFlags>
} = {}): {
  preset: Canvas2dPreset | undefined
  tools: ToolId[] | undefined
  features: Canvas2dFeatureFlags
} {
  const preset = isCanvas2dPreset(view.preset) ? view.preset : undefined
  const presetConfig = preset ? PRESETS[preset] : undefined
  const features = {
    ...CANVAS2D_DEFAULT_FEATURES,
    ...(presetConfig?.features ?? {}),
    ...(view.features ?? {}),
  }
  const tools = view.tools ?? presetConfig?.tools
  return {
    preset,
    tools: tools && tools.length > 0 ? [...tools] : undefined,
    features,
  }
}

// ── canvas2d 渲染路径判定(M3 R1:把分散在编辑器里的 4 路分支收成单一可测真相)──────────────
// canvas2d 历史上有 3 套硬分叉渲染路径(spatial bbox / pixel-layer mask / layered ps-lite)+ 空态 +
// 纯视口兜底,判定散在 Canvas2dEditor 的 4 个 if 里。本函数把"该走哪条路"抽成一处纯逻辑(优先级即
// 现有控制流顺序:spatial → 空态 → pixel-layer → layered → viewport),为后续路径合并(R2/R3)立地基,
// 同时让分支判定可单测(给输入断言 kind)。error 是相位覆盖态(非独立 kind),由 status.phase 另判。
export type Canvas2dSourceKind = 'spatial' | 'empty' | 'pixel-layer' | 'layered' | 'viewport'

export function resolveCanvas2dSourceKind(input: {
  /** geometry 字段映射 + 实体集都在 → spatial bbox 路径。 */
  hasGeometryAndEntities: boolean
  /** 宿主锚实体存在。 */
  hasEntity: boolean
  /** preset/features 解析出多图层合成开。 */
  featuresLayers: boolean
  /** 声明了 layer 实体桶名(layered 无锚也成立的条件之一)。 */
  hasLayerType: boolean
  /** 解析出有效像素层字段(pixelLayerField??selectionLayerField??maskField??rangeMaskField)。 */
  hasPixelLayerField: boolean
}): Canvas2dSourceKind {
  if (input.hasGeometryAndEntities) return 'spatial'
  // 空态守卫:无锚、且非(layered 且有层桶)、且无像素层字段 → 空/拖入(与现编辑器同序,先于 pixel-layer)。
  if (!input.hasEntity && !(input.featuresLayers && input.hasLayerType) && !input.hasPixelLayerField) return 'empty'
  if (input.hasPixelLayerField) return 'pixel-layer'
  if (input.featuresLayers) return 'layered'
  return 'viewport'
}
