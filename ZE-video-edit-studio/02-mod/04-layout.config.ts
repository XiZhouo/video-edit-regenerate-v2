/**
 * 02-mod/04-layout.config.ts — ZE-video-edit-studio 前端布局:**三区**。
 *
 *   左上 画布   custom[clip]         播放合成结果;按住拉框 + 不松手滚轮擦洗定区间
 *   左下 时间线 custom[clip]         图层栈(一行一片段,眼睛决定参不参与合成)+ 选段
 *   右   控制台 custom[send_profile] 写"改成什么"、发送;结果作为新图层插最上面
 *
 * 为什么是三个而不是规划画的五个(Design 标准 P2「默认最多 3 个编辑器」):
 * 五个区里**主判断只有一个** —— 这一段该改成什么、改出来的要不要。
 * 轨道并进时间线(时间线本就按图层分行);候选并进图层栈本身
 * (新结果就是一个新图层,不需要单独的候选池)。
 *
 * 三个都用 custom:通用槽(表格/表单/画廊)表达不了这个工具的核心动作 ——
 * 「在画面上圈一块」不能退化成填四个坐标数字。
 */

import type { ModLayoutConfig, LayoutNode, EditorClass } from '@ze/core/04-user-facing/01-layout'

const slot = (id: string, editor: EditorClass, bind: string[], viewState?: Record<string, unknown>, nameKey?: string): LayoutNode =>
  ({ kind: 'slot', id, nameKey, editor_class: editor, entity_bindings: bind, view_state: viewState })
const hsplit = (id: string, ratio: number, first: LayoutNode, second: LayoutNode): LayoutNode =>
  ({ kind: 'split', id, direction: 'horizontal', ratio, first, second })
const vsplit = (id: string, ratio: number, first: LayoutNode, second: LayoutNode): LayoutNode =>
  ({ kind: 'split', id, direction: 'vertical', ratio, first, second })

export const skillName = 'Video Edit Studio'
export const skillId = 'ze-video-edit-studio'
export const ambientBackground = 'static' as const

// ── 左上:画布 —— 主工作区。播放的是「可见图层从下往上叠」的结果。
// project 也绑进来:标注 bbox 是项目画布坐标,画面得按同一个比例摆才对得上。
const canvasSlot = slot('ves-canvas', 'custom',
  ['clip', 'annotation', 'guide_keyframe', 'asset', 'project'],
  { rendererKey: 'layerCanvas' }, 'mod.slot.canvas')

// ── 左下:时间线 —— 图层栈。关掉一行的眼睛,下面那层就露出来(这就是对比)。
const timelineSlot = slot('ves-timeline', 'custom',
  ['clip', 'annotation', 'guide_keyframe', 'reference_frame'],
  { rendererKey: 'layerTimeline' }, 'mod.slot.timeline')

// ── 右:控制台 —— 这次会发什么、改成什么、发。
const consoleSlot = slot('ves-console', 'custom',
  ['send_profile', 'clip', 'annotation', 'reference_frame', 'send_job'],
  { rendererKey: 'editConsole' }, 'mod.slot.console')

export const modLayout: ModLayoutConfig = {
  tree: hsplit('ves-root', 0.68,
    vsplit('ves-left', 0.62, canvasSlot, timelineSlot),
    consoleSlot,
  ),
}

export const newEntityDefaults: Record<string, { name: string; data: Record<string, unknown> }> = {
  // 新图层默认可见;真正的层序由 edit.send 算(插最上面)。
  clip: { name: '新图层', data: { layer: 0, visible: true, start: 0, duration: 4, sourceIn: 0, xKind: 'source' } },
  annotation: { name: '新引导', data: { kind: 'box', visible: true, label: '', number: 0, color: '#ff3b30', bboxX: 0, bboxY: 0, bboxW: 100, bboxH: 100, start: 0, duration: 0 } },
  guide_keyframe: { name: '位置', data: { parentId: '', time: 0, bboxX: 0, bboxY: 0, bboxW: 100, bboxH: 100, easing: 'linear' } },
  reference_frame: { name: '参考画面', data: { at: 0, enabled: true, label: '' } },
  send_profile: { name: '新发送', data: { mode: 1, provider: 'omni', strategy: 'annotated_video', model: 'gemini-omni-flash-preview', duration: 4, ratio: '16:9', paddingSec: 0.5 } },
  asset: { name: '新素材', data: { assetType: 'video', duration: 0, placeholder: true } },
}
