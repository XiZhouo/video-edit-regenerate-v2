export type WorkspaceSize = { w: number; h: number }
export type WorkspacePoint = { x: number; y: number }
export type WorkspaceOffset = { x: number; y: number }
export type WorkspaceTransform = { scale: number; off: WorkspaceOffset }

export function clampNumber(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

export function fitWorkspace(
  container: WorkspaceSize,
  content: WorkspaceSize,
  options: { maxScale?: number; padding?: number } = {},
): WorkspaceTransform {
  const maxScale = options.maxScale ?? 1
  const padding = options.padding ?? 0.94
  if (container.w <= 0 || container.h <= 0 || content.w <= 0 || content.h <= 0) {
    return { scale: 1, off: { x: 0, y: 0 } }
  }
  const scale = Math.min(maxScale, container.w / content.w, container.h / content.h) * padding
  return {
    scale,
    off: {
      x: (container.w - content.w * scale) / 2,
      y: (container.h - content.h * scale) / 2,
    },
  }
}

export function zoomWorkspaceAtPoint(
  transform: WorkspaceTransform,
  point: WorkspacePoint,
  deltaY: number,
  options: { minScale?: number; maxScale?: number; zoomInFactor?: number; zoomOutFactor?: number } = {},
): WorkspaceTransform {
  const minScale = options.minScale ?? 0.04
  const maxScale = options.maxScale ?? 32
  const zoomInFactor = options.zoomInFactor ?? 1.12
  const zoomOutFactor = options.zoomOutFactor ?? 0.89
  const oldScale = transform.scale || 1
  const nextScale = clampNumber(oldScale * (deltaY < 0 ? zoomInFactor : zoomOutFactor), minScale, maxScale)
  const world = screenToWorld(point, transform)
  return {
    scale: nextScale,
    off: {
      x: point.x - world.x * nextScale,
      y: point.y - world.y * nextScale,
    },
  }
}

export function screenToWorld(point: WorkspacePoint, transform: WorkspaceTransform): WorkspacePoint {
  const scale = transform.scale || 1
  return {
    x: (point.x - transform.off.x) / scale,
    y: (point.y - transform.off.y) / scale,
  }
}

export function clientPointInElement(
  client: WorkspacePoint,
  rect: Pick<DOMRect, 'left' | 'top'>,
): WorkspacePoint {
  return { x: client.x - rect.left, y: client.y - rect.top }
}

export function clampPointToSize(point: WorkspacePoint, size: WorkspaceSize): WorkspacePoint {
  return {
    x: clampNumber(point.x, 0, size.w),
    y: clampNumber(point.y, 0, size.h),
  }
}
