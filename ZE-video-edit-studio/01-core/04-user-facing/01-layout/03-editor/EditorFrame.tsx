/**
 * EditorFrame — 统一编辑器外壳(接管各 editor 复制粘贴的骨架)
 *
 * 协议: 03-css-rules/03-zones/SPEC.md(编辑器=组合功能区)。
 *
 * 现状:每个编辑器各自硬编码 `<div.ze-editor> <div.ze-editor__toolbar>… <body> {statusbar}`。
 * EditorFrame 把这层外壳收口成一个纯布局壳:**零业务逻辑、不 dispatch**,只摆 zone 槽。
 * 槽接收**已实例化的 zone 组件**(ReactNode)而非 props —— 编辑器保留对自身组装权,
 * 可逐个迁移(frame 不变)。复用现 `.ze-editor__*` CSS(不新增/不删 class → 不触发 CSS 契约 bump)。
 *
 * 编辑器自包含:只有 操作区(toolbar)+ 内容区(children)[+ statusbar]。**不含内嵌属性栏** ——
 * 属性面板是独立的 property 编辑器,在 L5 布局组合里与本编辑器并排,经全局选择联动。
 */

import type { CSSProperties, ReactNode, Ref } from 'react'

import { useScrollAffordance } from './lib/useScrollAffordance'
import type { EditorClass } from './types'

type EditorContentMode = 'surface' | 'pass-through'
type EditorContentLayout = 'plain' | 'stack' | 'padded-stack'

export interface EditorFrameProps {
  editorClass: EditorClass
  /** 承接局部快捷键的根 ref(useEditorShortcuts 挂这里)。 */
  rootRef?: Ref<HTMLDivElement>
  /**
   * @deprecated v2 起改用 header + zones(信息/控制二分,03-zones/SPEC §2)。迁移期保活:
   * 未迁编辑器继续传单条 toolbar;v2 编辑器传 header(身份)+zones(操作区行数组)。
   * 二者不应同时传;旧 toolbar 渲染于 header 之后、zones 之前。core 2.0.0 与 .ze-editor__toolbar 一并删。
   */
  toolbar?: ReactNode
  /** v2 身份行(HeaderZone)。条件渲染:多面板/有动态信息(计数·任务态)/编辑器级动作才传。 */
  header?: ReactNode
  /** v2 操作区行(已实例化 zone 数组,渲染序=rank:toolbar→filterbar→control[top]);调用方负责 key。 */
  zones?: ReactNode[]
  /** body 区:内容/主体 zone(content/info),editor 主数据。默认带 viewport 边界,保护画布/主工作面不裸奔。 */
  children: ReactNode
  /** 内容区**之后**的功能区行(已实例化 zone 数组,渲染在 .ze-editor__content 之后、__work 之内 =
   *  工作区底部、statusbar 横栏之上)。zones 在 content 之前(操作区),afterZones 在 content 之后
   *  (如内容下方的指令/对话输入条)—— 功能区是工作区里的有序容器,落位由声明决定、不靠专属槽
   *  (body=前 / footer=后,对齐 zones/types.ts ZoneRegion)。调用方负责 key。 */
  afterZones?: ReactNode[]
  /** afterZones 纵向尺寸语言:hug=内容高贴底(默认,= 旧裸渲染行为,如 PromptZone 一句输入条);
   *  share=与 content 按 grow 比例【瓜分剩余高】(随容器变矮等比缩,替代 mod 写死 vh/px)。 */
  afterSize?: 'hug' | 'share'
  /** afterSize='share' 时与 content 的纵向 flex-grow 比(content 恒 1)。缺省 1(各半);如 0.6 → 内容:对话≈1:0.6。 */
  afterGrow?: number
  /** 内容面模式:surface 是标准 viewport 边界;pass-through 仅在子级已有完整边界时保留 flex/滚动职责。 */
  contentMode?: EditorContentMode
  /** 内容区布局由 core 统一给,避免 mod 再套一层 ContentStack。 */
  contentLayout?: EditorContentLayout
  /** 内容区自身是否作为滚动归属点。 */
  contentScroll?: boolean
  /** footer 区:状态条 zone(StatusZone)。 */
  statusbar?: ReactNode
  /** 根容器额外 data-*(如 data-compare);仅有值的键渲染。 */
  dataAttrs?: Record<string, string | undefined>
  /** 默认 0(可聚焦,承接快捷键);个别 editor 可覆盖。 */
  tabIndex?: number
}

function pickDataAttrs(attrs?: Record<string, string | undefined>): Record<string, string> {
  const out: Record<string, string> = {}
  if (attrs) for (const [k, v] of Object.entries(attrs)) if (v !== undefined) out[k] = v
  return out
}

function EditorFrameBase({
  editorClass,
  rootRef,
  toolbar,
  header,
  zones,
  children,
  afterZones,
  afterSize,
  afterGrow,
  contentMode = 'surface',
  contentLayout = 'plain',
  contentScroll = false,
  statusbar,
  dataAttrs,
  tabIndex = 0,
}: EditorFrameProps): ReactNode {
  const contentRef = useScrollAffordance('y')
  return (
    <div
      className={`ze-editor ze-editor--${editorClass}`}
      data-editor-class={editorClass}
      ref={rootRef}
      tabIndex={tabIndex}
      {...pickDataAttrs(dataAttrs)}
    >
      {header}
      <div className="ze-editor__work">
        {toolbar}
        {zones}
        <div
          className="ze-editor__content"
          ref={contentRef}
          data-content-mode={contentMode === 'pass-through' ? 'pass-through' : undefined}
          data-content-layout={contentLayout === 'plain' ? undefined : contentLayout}
          data-content-scroll={contentScroll ? 'true' : undefined}
        >
          {children}
        </div>
        {afterZones ? (
          <div
            className="ze-editor__after"
            data-size={afterSize ?? 'hug'}
            style={afterGrow != null ? ({ ['--ze-after-grow']: String(afterGrow) } as CSSProperties) : undefined}
          >
            {afterZones}
          </div>
        ) : null}
      </div>
      {statusbar}
    </div>
  )
}

/** 统一空态(替换各 editor 各自的 .ze-editor__empty)。 */
function EditorFrameEmpty({ children }: { children: ReactNode }): ReactNode {
  return <div className="ze-editor__empty">{children}</div>
}

export const EditorFrame = Object.assign(EditorFrameBase, { Empty: EditorFrameEmpty })
