/**
 * localLink.ts — 「关联本地文件夹」的纯能力层(无 React)。
 *
 * 定位:客户端镜像 / 导出目标。server 仍独占 .data/ 写权;本模块只在用户经
 * showDirectoryPicker 选中的目录句柄内读写,默认单向 server→本地,绝不给 server
 * 开第二写者、绝不双向实时同步。
 *
 * 句柄持久化:FileSystemDirectoryHandle 可结构化克隆但不能 JSON 序列化,故落
 * IndexedDB(按 projectId 分槽),回访时 requestPermission() 恢复(F5)。
 *
 * 优雅降级(F9):localLinkSupported() 为 false 时,上层只暴露 F1/F2 导出导入,
 * 完全不触碰本模块的目录能力。
 */

const DB_NAME = 'ze-local-link'
const STORE = 'handles'
const DB_VERSION = 1
const MAX_MIRROR_PATH_LENGTH = 512
const MAX_MIRROR_PATH_DEPTH = 8
const MAX_MIRROR_SEGMENT_LENGTH = 128
const SAFE_MIRROR_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

/** 一个项目的关联记录:目录句柄 + 上次全量备份时间(F8 展示用)。 */
export type LocalLinkRecord = {
  handle: FileSystemDirectoryHandle
  lastBackupAt: number | null
}

/** F9:能力探测。缺 showDirectoryPicker 或 indexedDB 即视为不支持。 */
export function localLinkSupported(): boolean {
  return (
    typeof window !== 'undefined'
    && 'showDirectoryPicker' in window
    && typeof window.indexedDB !== 'undefined'
  )
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = window.indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE)
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error ?? new Error('indexeddb_open_failed'))
  })
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve()
    tx.onabort = () => reject(tx.error ?? new Error('indexeddb_tx_aborted'))
    tx.onerror = () => reject(tx.error ?? new Error('indexeddb_tx_error'))
  })
}

/** 读某项目的关联记录;无记录返回 null。 */
export async function loadLink(projectId: string): Promise<LocalLinkRecord | null> {
  const db = await openDb()
  try {
    const tx = db.transaction(STORE, 'readonly')
    const req = tx.objectStore(STORE).get(projectId)
    const value = await new Promise<unknown>((resolve, reject) => {
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error ?? new Error('indexeddb_get_failed'))
    })
    if (!value || typeof value !== 'object') return null
    const record = value as Partial<LocalLinkRecord>
    if (!record.handle || record.handle.kind !== 'directory') return null
    return {
      handle: record.handle as FileSystemDirectoryHandle,
      lastBackupAt: typeof record.lastBackupAt === 'number' ? record.lastBackupAt : null,
    }
  } finally {
    db.close()
  }
}

/** 写某项目的关联记录(link 时存句柄;backup 后回写 lastBackupAt)。 */
export async function saveLink(projectId: string, record: LocalLinkRecord): Promise<void> {
  const db = await openDb()
  try {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).put(record, projectId)
    await txDone(tx)
  } finally {
    db.close()
  }
}

/** 解除关联:删句柄记录(F6 unlink)。不动本地已写出的文件。 */
export async function deleteLink(projectId: string): Promise<void> {
  const db = await openDb()
  try {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).delete(projectId)
    await txDone(tx)
  } finally {
    db.close()
  }
}

/** F3:弹出目录选择器,取读写目录句柄。用户取消会抛 AbortError,由上层吞掉。 */
export function pickDirectory(): Promise<FileSystemDirectoryHandle> {
  return window.showDirectoryPicker({ id: 'ze-local-link', mode: 'readwrite' })
}

/** F5:查询/申请句柄的读写权限。granted 返回 true,其余返回 false(不抛)。 */
export async function ensurePermission(
  handle: FileSystemDirectoryHandle,
  { request }: { request: boolean },
): Promise<boolean> {
  const descriptor = { mode: 'readwrite' as const }
  try {
    if ((await handle.queryPermission(descriptor)) === 'granted') return true
    if (!request) return false
    return (await handle.requestPermission(descriptor)) === 'granted'
  } catch {
    return false
  }
}

/**
 * F7:在关联目录内选一个 .ze-pack 文件(startIn 定位到该目录),返回其 File。
 * 用户取消返回 null。拿到 File 后交回既有 manifest.preview/import 提案链。
 */
export async function pickImportFile(handle: FileSystemDirectoryHandle): Promise<File | null> {
  try {
    const [fileHandle] = await window.showOpenFilePicker({
      id: 'ze-local-link',
      startIn: handle,
      multiple: false,
      types: [{ description: 'ZE pack', accept: { 'application/zip': ['.ze-pack', '.zip', '.json'] } }],
    })
    return fileHandle ? await fileHandle.getFile() : null
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return null
    throw error
  }
}

/**
 * 把镜像内部路径收敛为封闭的相对路径段。
 *
 * 这不是通用文件管理器：Local Link 只写标准自己生成的 document/asset 路径，
 * 因此拒绝绝对路径、空段、`.` / `..`、反斜杠、控制字符、超长和任意 Unicode
 * 文件名。即使服务端文档被污染，也不能借扩展名把写入位置改到 assets 之外。
 */
export function mirrorPathParts(relPath: string): string[] {
  if (
    typeof relPath !== 'string'
    || relPath.length === 0
    || relPath.length > MAX_MIRROR_PATH_LENGTH
    || relPath.startsWith('/')
    || relPath.endsWith('/')
    || relPath.includes('\\')
  ) {
    throw new Error('local_link_invalid_path')
  }
  const parts = relPath.split('/')
  if (parts.length === 0 || parts.length > MAX_MIRROR_PATH_DEPTH) {
    throw new Error('local_link_invalid_path')
  }
  for (const part of parts) {
    if (
      part.length === 0
      || part.length > MAX_MIRROR_SEGMENT_LENGTH
      || part === '.'
      || part === '..'
      || !SAFE_MIRROR_SEGMENT.test(part)
    ) {
      throw new Error('local_link_invalid_path')
    }
  }
  return parts
}

/** 写一个文件到目录(relPath 可含子目录,用 '/' 分隔;缺失子目录自动创建)。 */
export async function writeFile(
  root: FileSystemDirectoryHandle,
  relPath: string,
  bytes: BufferSource,
): Promise<void> {
  const parts = mirrorPathParts(relPath)
  const name = parts.pop()
  if (!name) throw new Error('local_link_invalid_path')
  let dir = root
  for (const part of parts) {
    dir = await dir.getDirectoryHandle(part, { create: true })
  }
  const fileHandle = await dir.getFileHandle(name, { create: true })
  const writable = await fileHandle.createWritable()
  try {
    await writable.write(bytes)
  } finally {
    await writable.close()
  }
}

/** 探测目录内是否已存在某文件(增量镜像:已存在的内容寻址 blob 不再重复拉取)。 */
export async function fileExists(
  root: FileSystemDirectoryHandle,
  relPath: string,
): Promise<boolean> {
  const parts = mirrorPathParts(relPath)
  const name = parts.pop()
  if (!name) return false
  let dir = root
  try {
    for (const part of parts) dir = await dir.getDirectoryHandle(part)
    await dir.getFileHandle(name)
    return true
  } catch {
    return false
  }
}
