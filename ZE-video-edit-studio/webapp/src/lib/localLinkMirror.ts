/**
 * localLinkMirror.ts — 单向 server→本地增量镜像(F4 自动 / F6 全量)。
 *
 * 只读 server(getDocument + asset ticket + asset blob),把快照写进用户选中的目录
 * 句柄。绝不写 .data、绝不发 mutation、绝不双向同步 —— 单写者不变量由此保证。
 *
 * 产物布局:
 *   <dir>/document.json     ← 当前项目 canonical 快照(meta + entities + cursor)
 *   <dir>/assets/<sha256><ext>  ← 文档引用到的全部内容寻址 blob
 *
 * 增量:blob 以 sha256 命名(内容寻址),已存在的直接跳过,不重复下载。
 */

import { assetUrl, getDocument, requestAssetTickets } from './api'
import { collectAssetSha256s } from './useMediaTickets'
import { fileExists, writeFile, type LocalLinkRecord } from './localLink'

const TICKET_BATCH = 256
const SHA256 = /^[0-9a-f]{64}$/
const SAFE_ASSET_EXTENSION = /^(?:\.[a-z0-9]{1,16}){1,2}$/

/** 服务端扩展名只影响镜像文件名；任何路径字符或异常长度都降级为空扩展名。 */
export function normalizeAssetExtension(value: unknown): string {
  if (typeof value !== 'string') return ''
  const normalized = value.toLowerCase()
  return normalized.length <= 34 && SAFE_ASSET_EXTENSION.test(normalized)
    ? normalized
    : ''
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) throw new Error('local_link_crypto_unavailable')
  const digest = await subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

/** 从文档快照里收全部资产引用(sha256 → ext),用于命名本地 blob 文件。 */
function collectAssetRefs(value: unknown): Map<string, string> {
  const refs = new Map<string, string>()
  const seen = new WeakSet<object>()
  const walk = (item: unknown): void => {
    if (!item || typeof item !== 'object') return
    if (seen.has(item)) return
    seen.add(item)
    if (Array.isArray(item)) {
      for (const child of item) walk(child)
      return
    }
    const record = item as Record<string, unknown>
    if (typeof record.sha256 === 'string' && SHA256.test(record.sha256)) {
      const ext = normalizeAssetExtension(record.ext)
      // 首个安全扩展名胜出；先见到空扩展名时允许后续安全声明补齐。
      if (!refs.has(record.sha256) || (refs.get(record.sha256) === '' && ext !== '')) {
        refs.set(record.sha256, ext)
      }
    }
    for (const child of Object.values(record)) walk(child)
  }
  walk(value)
  return refs
}

export type MirrorResult = {
  documentWritten: boolean
  assetsWritten: number
  assetsSkipped: number
  assetsMissing: number
}

/**
 * 把当前项目快照镜像进目录句柄。调用前上层须已确认句柄权限(F5)。
 * 任一步失败向上抛,由调用方落到 F8 的错误态,不静默。
 */
export async function mirrorToFolder(
  record: LocalLinkRecord,
  projectId: string,
  generation: number,
): Promise<MirrorResult> {
  if (!Number.isSafeInteger(generation) || generation <= 0) {
    throw new Error('local_link_invalid_generation')
  }
  const handle = record.handle
  const snapshot = await getDocument(projectId)
  if (snapshot.meta.generation !== generation) {
    throw new Error('local_link_generation_changed')
  }
  if (!Number.isSafeInteger(snapshot.meta.version) || snapshot.meta.version <= 0) {
    throw new Error('local_link_document_version_invalid')
  }
  const documentBytes = new TextEncoder().encode(JSON.stringify(snapshot, null, 2))

  const refs = collectAssetRefs(snapshot.entities)
  // useMediaTickets 的收集器是权威口径;与本地 refs 取交集,避免漏收嵌套形态。
  for (const sha of collectAssetSha256s(snapshot.entities)) {
    if (!refs.has(sha)) refs.set(sha, '')
  }

  let assetsWritten = 0
  let assetsSkipped = 0
  let assetsMissing = 0

  // 先算出真正需要下载的 sha(跳过本地已存在的内容寻址文件)。
  const pending: string[] = []
  for (const [sha, ext] of refs) {
    if (await fileExists(handle, `assets/${sha}${ext}`)) assetsSkipped += 1
    else pending.push(sha)
  }

  for (let offset = 0; offset < pending.length; offset += TICKET_BATCH) {
    const batchShas = pending.slice(offset, offset + TICKET_BATCH)
    const batch = await requestAssetTickets(projectId, generation, batchShas)
    for (const sha of batchShas) {
      const ticket = batch.tickets[sha]
      if (!ticket) { assetsMissing += 1; continue }
      const url = assetUrl({ sha256: sha }, projectId, generation, ticket)
      if (!url) { assetsMissing += 1; continue }
      const response = await fetch(url, { cache: 'no-store', credentials: 'include' })
      if (!response.ok) { assetsMissing += 1; continue }
      const buffer = await response.arrayBuffer()
      if (await sha256Hex(buffer) !== sha) {
        throw new Error('local_link_asset_digest_mismatch')
      }
      await writeFile(handle, `assets/${sha}${refs.get(sha) ?? ''}`, buffer)
      assetsWritten += 1
    }
  }

  // document.json 是本次镜像的可见提交点。资产不完整时不推进它，也不让上层
  // 更新 lastBackupAt；已经写入的内容寻址 blob 可被下一次安全复用。
  if (assetsMissing > 0) throw new Error('local_link_assets_incomplete')
  await writeFile(handle, 'document.json', documentBytes)

  return { documentWritten: true, assetsWritten, assetsSkipped, assetsMissing }
}
