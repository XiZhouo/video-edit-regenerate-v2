/**
 * spatialTypes — 2D 空间编辑器共享类型(框架级)
 *
 * 图像空间(image space)= entity.data 里存的几何单位(原点=图左上,px)。
 * 屏幕空间(screen space)= 容器内像素。Viewport 在两者间换算。
 */

export type Vec2 = { x: number; y: number }

/** 图像空间矩形 */
export type Box = { x: number; y: number; w: number; h: number }
/** 矩形 + 可选旋转(度,绕 box 中心)*/
export type Geometry = Box & { rotation?: number }

/** 屏幕<->图像变换:scale = 图像 px → 屏幕 px;tx/ty = 屏幕 px 平移 */
export type Viewport = { scale: number; tx: number; ty: number }

/** entity.data 里几何字段名映射 —— mod 经 view_state 声明,编辑器据此读写 */
export type GeometryFieldMap =
  | { kind: 'flat'; x: string; y: string; w: string; h: string; rotation?: string }
  | { kind: 'object'; field: string }   // entity.data[field] = {x,y,w,h,rotation?}

export type HandleId = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w' | 'rotate'

export type GizmoConstraints = {
  minW: number
  minH: number
  maxW?: number
  maxH?: number
  rotationEnabled: boolean
  /** 固定宽高比(>0 时角点缩放恒锁此比;Shift 临时锁用 start 比例)*/
  aspect?: number
}

export const DEFAULT_CONSTRAINTS: GizmoConstraints = {
  minW: 8, minH: 8, rotationEnabled: false,
}
