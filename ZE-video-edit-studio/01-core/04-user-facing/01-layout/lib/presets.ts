/**
 * presets — editor-combination 层:匹配真实工作流形态的 LayoutTree 预设(纯函数)。
 *
 * mod 选一个预设 + 绑实体,就得到 3-4 面板布局,不用手写 LayoutNode 树或 App.tsx 面板
 * (杀掉 7774 行 monolith 的结构性手段)。预设返回的就是 LayoutRenderer 直接吃的可序列化树。
 *
 * 四种形态(survey 自现有 skill 的真实工作流):
 *   linearProcess  Input | Main | Output       (eraser / image-to-3d / linework)
 *   catalogBrowse  List | Workbench | Inspector (seek-find / libraries)
 *   singleCanvas   Canvas (+ Params)            (绘制类工具)
 *   pipelineBatch  Config | Results+进度        (批量生成;Log 走 footer)
 */

import type { EditorClass, LayoutNode } from '../types'

export type SlotSpec = {
  editor: EditorClass
  bind: string[]
  viewState?: Record<string, unknown>
  /** 可译编辑器名(槽位横栏标题,指向 09-i18n 词表;i18n_audit slot-name 门禁要求每槽必填)。 */
  nameKey?: string
}

function slot(id: string, s: SlotSpec): LayoutNode {
  return {
    kind: 'slot', id, nameKey: s.nameKey,
    editor_class: s.editor,
    entity_bindings: s.bind,
    view_state: s.viewState,
  }
}

function hsplit(id: string, ratio: number, first: LayoutNode, second: LayoutNode): LayoutNode {
  return { kind: 'split', id, direction: 'horizontal', ratio, first, second }
}

function vsplit(id: string, ratio: number, first: LayoutNode, second: LayoutNode): LayoutNode {
  return { kind: 'split', id, direction: 'vertical', ratio, first, second }
}

export function linearProcess(p: { input: SlotSpec; main: SlotSpec; output: SlotSpec }): LayoutNode {
  return hsplit('lp-0', 0.26, slot('lp-input', p.input),
    hsplit('lp-1', 0.62, slot('lp-main', p.main), slot('lp-output', p.output)))
}

export function catalogBrowse(p: { list: SlotSpec; workbench: SlotSpec; inspector: SlotSpec }): LayoutNode {
  return hsplit('cb-0', 0.22, slot('cb-list', p.list),
    hsplit('cb-1', 0.72, slot('cb-workbench', p.workbench), slot('cb-inspector', p.inspector)))
}

export function singleCanvas(p: { canvas: SlotSpec; params?: SlotSpec }): LayoutNode {
  if (!p.params) return slot('sc-canvas', p.canvas)
  return hsplit('sc-0', 0.74, slot('sc-canvas', p.canvas), slot('sc-params', p.params))
}

export function pipelineBatch(p: { config: SlotSpec; results: SlotSpec }): LayoutNode {
  return hsplit('pb-0', 0.3, slot('pb-config', p.config), slot('pb-results', p.results))
}

/** layeredCanvas 画布-图层-属性(PS 图层工作台):图层列表 | 画布 | 属性。三个**独立**编辑器,
 *  都绑同一 entity_type(layer 实体,类型库 layer@1);选择按 type 全局共享 → 列表↔画布↔属性联动。
 *  法定形态(1.27.0):layers 槽首选 **gallery 卡片列表**(compact-list + visibilityField/
 *  lockedField/reorderCommand;table 仍合法);canvas 槽 = canvas2d{preset,layerType}(layered
 *  合成,活动层=selection);inspector = 独立 form 编辑器,**不内嵌进画布**(SPEC §2.1b 三律)。 */
export function layeredCanvas(p: { layers: SlotSpec; canvas: SlotSpec; inspector: SlotSpec }): LayoutNode {
  return hsplit('lyc-0', 0.22, slot('lyc-layers', p.layers),
    hsplit('lyc-1', 0.7, slot('lyc-canvas', p.canvas), slot('lyc-inspector', p.inspector)))
}

/** encyclopedia 百科页(目录-正文-图鉴-属性):catalog(outline 节点树)| document(正文块) |
 *  [gallery(图鉴缩略图) / form(块属性)]。四个**独立**编辑器经全局 selection[nodeType]/selection[block]
 *  联动(SPEC §2.1b 三律,零直连):catalog 选节点 → document 渲该节点正文 + gallery 显其图;选块 →
 *  form 改块属性。gallery / inspector 可选(缺则退化为目录-正文两栏 / 三栏)。猫猫大百科 / 文化区域
 *  旅游百科同形态——差异(猫色网格 / 区域海报·9 天·经纬度)全在 mod 的 entity 类型与 view_state。 */
export function encyclopedia(
  p: { catalog: SlotSpec; detail: SlotSpec; gallery?: SlotSpec; inspector?: SlotSpec },
): LayoutNode {
  const right = p.gallery && p.inspector
    ? vsplit('enc-right', 0.55, slot('enc-gallery', p.gallery), slot('enc-inspector', p.inspector))
    : p.gallery ? slot('enc-gallery', p.gallery)
      : p.inspector ? slot('enc-inspector', p.inspector)
        : null
  const detail = slot('enc-detail', p.detail)
  const main = right ? hsplit('enc-1', 0.62, detail, right) : detail
  return hsplit('enc-0', 0.2, slot('enc-catalog', p.catalog), main)
}

export type PresetName = 'linear_process' | 'catalog_browse' | 'single_canvas' | 'pipeline_batch' | 'layered_canvas' | 'encyclopedia'

export const PRESETS = {
  linear_process: linearProcess,
  catalog_browse: catalogBrowse,
  single_canvas: singleCanvas,
  pipeline_batch: pipelineBatch,
  layered_canvas: layeredCanvas,
  encyclopedia,
} as const
