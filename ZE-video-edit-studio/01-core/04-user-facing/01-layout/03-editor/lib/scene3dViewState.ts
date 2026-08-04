/**
 * Scene3D personal camera view state.
 *
 * The store accepts renderer-specific JSON snapshots, but deliberately rejects engine objects,
 * cycles, functions, non-finite numbers and oversized trees. A renderer must emit a small plain
 * snapshot, never a THREE.Camera or another live mutable instance.
 */

import type { ViewStateSliceCodec } from '@ze/core/04-user-facing/05-preferences'

export const SCENE3D_PERSONAL_CAMERA_SLICE = 'scene3d.personal-camera'
export const SCENE3D_PERSONAL_CAMERA_VERSION = 1

const MAX_DEPTH = 8
const MAX_NODES = 512
const MAX_ARRAY_ITEMS = 64
const MAX_OBJECT_KEYS = 64
const MAX_KEY_LENGTH = 128
const MAX_STRING_LENGTH = 2048
const MAX_SERIALIZED_BYTES = 16 * 1024
const INVALID = Symbol('invalid-scene3d-camera')

export type Scene3dJsonPrimitive = null | boolean | number | string
export type Scene3dJsonValue = Scene3dJsonPrimitive | Scene3dJsonValue[] | { [key: string]: Scene3dJsonValue }
export type Scene3dCameraSnapshot = { [key: string]: Scene3dJsonValue }
export type Scene3dPersonalCameraState = { camera: Scene3dCameraSnapshot | null }
export type Scene3dOrbitCamera = {
  position: [number, number, number]
  target: [number, number, number]
  up?: [number, number, number]
  zoom?: number
  fov?: number
}

function plainRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  try {
    const prototype = Object.getPrototypeOf(value)
    return prototype === Object.prototype || prototype === null
      ? value as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function cloneJson(
  value: unknown,
  depth: number,
  seen: WeakSet<object>,
  budget: { nodes: number },
): Scene3dJsonValue | typeof INVALID {
  budget.nodes += 1
  if (budget.nodes > MAX_NODES || depth > MAX_DEPTH) return INVALID
  if (value === null || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : INVALID
  if (typeof value === 'string') return value.length <= MAX_STRING_LENGTH ? value : INVALID
  if (typeof value !== 'object') return INVALID
  if (seen.has(value)) return INVALID
  seen.add(value)
  try {
    if (Array.isArray(value)) {
      if (value.length > MAX_ARRAY_ITEMS) return INVALID
      const out: Scene3dJsonValue[] = []
      for (const item of value) {
        const cloned = cloneJson(item, depth + 1, seen, budget)
        if (cloned === INVALID) return INVALID
        out.push(cloned)
      }
      return out
    }
    const record = plainRecord(value)
    if (!record) return INVALID
    const keys = Object.keys(record)
    if (keys.length > MAX_OBJECT_KEYS || Object.getOwnPropertySymbols(record).length > 0) return INVALID
    const descriptors = Object.getOwnPropertyDescriptors(record)
    const out: { [key: string]: Scene3dJsonValue } = {}
    for (const key of keys) {
      if (key.length > MAX_KEY_LENGTH) return INVALID
      const descriptor = descriptors[key]
      if (!descriptor || !('value' in descriptor)) return INVALID
      const cloned = cloneJson(descriptor.value, depth + 1, seen, budget)
      if (cloned === INVALID) return INVALID
      out[key] = cloned
    }
    return out
  } finally {
    seen.delete(value)
  }
}

/** Return an immutable-by-copy, bounded JSON-safe snapshot or null when the renderer payload is unsafe. */
export function normalizeScene3dCameraSnapshot(value: unknown): Scene3dCameraSnapshot | null {
  try {
    if (!plainRecord(value)) return null
    const cloned = cloneJson(value, 0, new WeakSet(), { nodes: 0 })
    if (cloned === INVALID || !plainRecord(cloned)) return null
    const serialized = JSON.stringify(cloned)
    if (new TextEncoder().encode(serialized).byteLength > MAX_SERIALIZED_BYTES) return null
    return cloned as Scene3dCameraSnapshot
  } catch {
    return null
  }
}

export const scene3dPersonalCameraCodec: ViewStateSliceCodec<Scene3dPersonalCameraState> = {
  version: SCENE3D_PERSONAL_CAMERA_VERSION,
  decode: (value) => {
    const record = plainRecord(value)
    if (!record) return null
    const camera = normalizeScene3dCameraSnapshot(record.camera)
    return camera ? { camera } : null
  },
  migrate: (value, fromVersion) => {
    if (fromVersion !== 0) return null
    const record = plainRecord(value)
    const camera = normalizeScene3dCameraSnapshot(record && 'camera' in record ? record.camera : value)
    return camera ? { camera } : null
  },
}

function finiteVector(value: unknown): [number, number, number] | null {
  if (Array.isArray(value) && value.length === 3 && value.every((part) => typeof part === 'number' && Number.isFinite(part))) {
    return [value[0] as number, value[1] as number, value[2] as number]
  }
  const record = plainRecord(value)
  if (!record) return null
  const { x, y, z } = record
  return typeof x === 'number' && Number.isFinite(x)
    && typeof y === 'number' && Number.isFinite(y)
    && typeof z === 'number' && Number.isFinite(z)
    ? [x, y, z]
    : null
}

/** Canonical orbit pose used by the bundled reference renderer; other renderers may use other JSON shapes. */
export function asScene3dOrbitCamera(value: unknown): Scene3dOrbitCamera | null {
  const record = plainRecord(value)
  if (!record) return null
  const position = finiteVector(record.position)
  const target = finiteVector(record.target)
  if (!position || !target) return null
  const up = record.up === undefined ? undefined : finiteVector(record.up)
  if (record.up !== undefined && !up) return null
  const zoom = record.zoom === undefined
    ? undefined
    : typeof record.zoom === 'number' && Number.isFinite(record.zoom) && record.zoom > 0 ? record.zoom : null
  const fov = record.fov === undefined
    ? undefined
    : typeof record.fov === 'number' && Number.isFinite(record.fov) && record.fov > 0 && record.fov < 180 ? record.fov : null
  if (zoom === null || fov === null) return null
  return { position, target, ...(up ? { up } : {}), ...(zoom ? { zoom } : {}), ...(fov ? { fov } : {}) }
}
