/**
 * guideTargets.ts — 引导 target 串 → 实时 DOM 锚点(纯函数,零副作用)。
 *
 * target 语法(与 GUIDED-TOUR.md §4 调和一致):
 *   cmd:<id>            → [data-ze-cmd="<id>"]            (命令按钮,相1 已加)
 *   region:<id>         → [data-ze-region="<id>"]         (语义区,已有)
 *   guide:<id>          → [data-ze-guide-target="<id>"]   (呈现锚点,按需注入)
 *   control:<id>        → [data-ze-control="<id>"]        (控件,opt-in)
 *   editor:<slotId>     → [data-ze-editor-slot="<slotId>"](编辑器槽,opt-in)
 *   entity:<type>:<id>  → [data-ze-entity-type][data-ze-entity-id]
 *   entity:<type>:@selected → 取当前 selection 的该类型 active id
 *   tool:<ref>          → 工具级,无点锚点(精灵居中说)
 *   vision:<ref>        → 截图视觉兜底(ZE-screen-guide;本相留位,返回 null)
 *
 * 坐标永远经 getBoundingClientRect 现算(抗布局/缩放/滚动);找不到锚点即返回 null,
 * 由 GuideLayer 优雅降级为「只说不指」。
 */

export type GuideSelection = Record<string, string | null> | undefined

function esc(value: string): string {
  const css = (globalThis as { CSS?: { escape?: (s: string) => string } }).CSS
  if (css?.escape) return css.escape(value)
  // 保守回退:只允许安全字符,其余剔除(避免注入非法选择器)
  return value.replace(/[^\w:.-]/g, '')
}

function firstEl(selector: string): HTMLElement | null {
  try {
    return document.querySelector<HTMLElement>(selector)
  } catch {
    return null
  }
}

/** target 串 → DOM 元素(找不到 → null)。 */
export function resolveGuideAnchor(
  target: string,
  selection?: GuideSelection,
): HTMLElement | null {
  const raw = (target ?? '').trim()
  if (!raw) return null
  const sep = raw.indexOf(':')
  if (sep < 0) return null
  const prefix = raw.slice(0, sep)
  const rest = raw.slice(sep + 1)
  if (!rest) return null

  switch (prefix) {
    case 'cmd':
      return firstEl(`[data-ze-cmd="${esc(rest)}"]`)
    case 'region':
      return firstEl(`[data-ze-region="${esc(rest)}"]`)
    case 'guide':
      return firstEl(`[data-ze-guide-target="${esc(rest)}"]`)
    case 'control':
      return firstEl(`[data-ze-control="${esc(rest)}"]`)
    case 'editor':
      return firstEl(`[data-ze-editor-slot="${esc(rest)}"]`)
    case 'entity': {
      const inner = rest.indexOf(':')
      // W1-2:**无实例 id 的 `entity:<type>`** —— 内核投影 key_flows 时就是这个形状
      //(semantic_target{kind:'entity', ref:'image_task'} 本来就不带实例)。此前这里
      // 直接 return null,于是 foundry 那条 9 步 tour 里 4 个 entity 步全落空。
      // 语义应是「这类东西」:选中的那个优先,没选中就指第一个 —— 教学要指的是
      // 「任务卡长这样」,不是某一张特定的卡。
      if (inner < 0) {
        const type = rest
        const selected = selection?.[type]
        if (selected) {
          const el = firstEl(`[data-ze-entity-type="${esc(type)}"][data-ze-entity-id="${esc(selected)}"]`)
          if (el) return el
        }
        return firstEl(`[data-ze-entity-type="${esc(type)}"][data-ze-entity-id]`)
      }
      const type = rest.slice(0, inner)
      let id = rest.slice(inner + 1)
      if (id === '@selected') {
        const selected = selection?.[type]
        if (!selected) return null
        id = selected
      }
      if (id === '@first') {
        return firstEl(`[data-ze-entity-type="${esc(type)}"][data-ze-entity-id]`)
      }
      if (!type || !id) return null
      return firstEl(`[data-ze-entity-type="${esc(type)}"][data-ze-entity-id="${esc(id)}"]`)
    }
    case 'tool':
    case 'vision':
    default:
      // 工具级 / 视觉兜底:无点锚点,精灵居中说。
      return null
  }
}

/** 呈现覆盖:step.presentation 若声明 anchor/target 串,优先它(逻辑层恒为 {},此为前端覆盖位)。 */
export function anchorOverride(presentation: Record<string, unknown> | undefined): string {
  if (!presentation) return ''
  const v = presentation.anchor ?? presentation.target
  return typeof v === 'string' ? v : ''
}

/* ═══════════ mod 锚点映射表(W1-2)═══════════
 *
 * 为什么需要它:内核把 key_flows 的 `semantic_target{kind, ref}` 投影成 target 串
 * (`control:control.plan` 之类),那是**语义**——「计划这件事在哪做」。而 DOM 上
 * 存在的是具体节点。`control:` / `guide:` / `editor:` 三类按 GUIDED-TOUR.md §4 属于
 * **呈现覆盖层**,规定「由前端填进每步的 presentation」——但此前没有任何前端填过它,
 * 于是这三类目标在全舰队恒解析成 null,精灵只说不指。
 *
 * `entity:` 与 `cmd:` / `region:` 不同:前者由框架统一产出(ThumbnailGrid/TableZone 的
 * data-ze-entity-*,W1-2 同批补),后两者本来就有真实产出方,都不需要走这张表。
 *
 * 只有 mod 知道自己的 `control.plan` 到底是哪个按钮 —— 所以这是注册表不是硬编码:
 * mod 在 bootstrap 里 registerGuideAnchors({...}),框架零业务知识。
 */
let _modAnchors: Readonly<Record<string, string>> = {}

/** mod 声明:语义 target 串 → 真实可解析的 target 串。重复注册以最后一次为准(热更友好)。 */
export function registerGuideAnchors(map: Record<string, string>): void {
  _modAnchors = Object.freeze({ ..._modAnchors, ...map })
}

/** 已注册的映射(只读;供门禁与自检读)。 */
export function guideAnchorMap(): Readonly<Record<string, string>> {
  return _modAnchors
}

/**
 * 解析优先级:作者在步骤上写死的 presentation.anchor > mod 注册表 > 内核语义 target。
 * 前两者都空时原样返回语义串 —— 它可能本来就能解析(如 cmd:/region:/entity:)。
 */
export function effectiveTarget(
  semantic: string,
  presentation?: Record<string, unknown>,
): string {
  return anchorOverride(presentation) || _modAnchors[semantic] || semantic
}
