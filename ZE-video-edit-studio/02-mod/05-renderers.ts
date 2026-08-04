/**
 * 02-mod/05-renderers.ts — 自定义面板渲染器表(框架标准 seam)。
 *
 * 三区 = 三个 editor,各自的责任边界:
 *   layerCanvas   左上 · 画布   —— 播放合成结果;按住拉框 + 不松手滚轮擦洗定区间
 *   layerTimeline 左下 · 时间线 —— 图层栈(一行一片段,👁 决定参不参与合成)+ 选段
 *   editConsole   右   · 控制台 —— 写"改成什么"、发送;结果作为新图层插最上面
 */
import type { ComponentType } from 'react'
import LayerCanvas from './05-code/renderers/LayerCanvas'
import LayerTimeline from './05-code/renderers/LayerTimeline'
import EditConsole from './05-code/renderers/EditConsole'

export const renderers: Record<string, ComponentType<any>> = {
  layerCanvas: LayerCanvas,
  layerTimeline: LayerTimeline,
  editConsole: EditConsole,
}
