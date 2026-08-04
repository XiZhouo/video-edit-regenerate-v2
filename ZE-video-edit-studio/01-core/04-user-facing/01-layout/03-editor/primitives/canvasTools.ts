/**
 * canvasTools — 画布工具注册表(开集)+ 笔画记录类型(框架级,opt-in)
 *
 * 这是**可选**能力:canvas2d 模板默认只 pan/select,不挂这套绘制工具栏。需要画笔 / 框选 /
 * 套索 / SAM 点的 mod 才显式传 tools=[...] 给 Canvas2dEditor 开 CanvasToolbar。
 * 笔画经 onStroke 回调交给 editor → dispatch command;绝不在 editor 内直接 mutate entity。
 */

export type ToolId =
  | 'pan' | 'select' | 'transform' | 'brush' | 'eraser'
  | 'text' | 'crop' | 'filter' | 'rect' | 'ellipse'
  | 'lasso' | 'polygon' | 'wand' | 'sam-points' | 'export'

export type ToolDef = {
  id: ToolId
  icon: string
  /** i18n key(在 05-i18n 的 tool.* 下);消费处(CanvasToolbar)用 t(labelKey) 取译文 */
  labelKey: string
  /** 该工具产出的笔画类型(给 onStroke 标注)*/
  strokeKind: 'none' | 'freehand' | 'shape' | 'path' | 'points'
}

/** 工具注册表(开集:mod 可补自定义 ToolId 的 ToolDef)。 */
export const CANVAS_TOOLS: Record<ToolId, ToolDef> = {
  pan:          { id: 'pan',        icon: 'pan', labelKey: 'tool.pan', strokeKind: 'none' },
  select:       { id: 'select',     icon: 'marquee', labelKey: 'tool.select', strokeKind: 'none' },
  transform:    { id: 'transform',  icon: 'transform', labelKey: 'tool.transform', strokeKind: 'none' },
  brush:        { id: 'brush',      icon: 'brush', labelKey: 'tool.brush', strokeKind: 'freehand' },
  eraser:       { id: 'eraser',     icon: 'eraser', labelKey: 'tool.eraser', strokeKind: 'freehand' },
  text:         { id: 'text',       icon: 'text', labelKey: 'tool.text', strokeKind: 'none' },
  crop:         { id: 'crop',       icon: 'crop', labelKey: 'tool.crop', strokeKind: 'shape' },
  filter:       { id: 'filter',     icon: 'filter', labelKey: 'tool.filter', strokeKind: 'none' },
  rect:         { id: 'rect',       icon: 'rect', labelKey: 'tool.rect', strokeKind: 'shape' },
  ellipse:      { id: 'ellipse',    icon: 'ellipse', labelKey: 'tool.ellipse', strokeKind: 'shape' },
  lasso:        { id: 'lasso',      icon: 'lasso', labelKey: 'tool.lasso', strokeKind: 'path' },
  polygon:      { id: 'polygon',    icon: 'polygon', labelKey: 'tool.polygon', strokeKind: 'path' },
  wand:         { id: 'wand',       icon: 'wand', labelKey: 'tool.wand', strokeKind: 'points' },
  'sam-points': { id: 'sam-points', icon: 'points', labelKey: 'tool.samPoints', strokeKind: 'points' },
  export:       { id: 'export',     icon: 'download', labelKey: 'tool.export', strokeKind: 'none' },
}

export type StrokePoint = { x: number; y: number }
export type StrokeRecord = {
  tool: ToolId
  kind: ToolDef['strokeKind']
  points: StrokePoint[]
  closed: boolean
  meta?: Record<string, unknown>
}
