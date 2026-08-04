/**
 * useLocalLink — 「关联本地文件夹」的 React 编排层。
 *
 * 把纯能力(localLink / localLinkMirror)接成一个状态机 + 四个动作
 * (link / unlink / backupNow / importFolder),供 Header 设置区(F8)消费。
 * 这些动作是纯客户端能力(浏览器句柄,server 无从触达),不注册为 server 命令;
 * 唯一落到 server 的是 importFolder 末端复用的既有 manifest.preview/import 命令。
 *
 * 自动镜像(F4):不新开 SSE —— openEventStream 已由 useDocument 驱动 meta.version
 * 递增,本 hook 以 documentVersion 作为「有已提交变更」的信号,防抖后单向增量镜像。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  deleteLink,
  ensurePermission,
  loadLink,
  localLinkSupported,
  pickDirectory,
  pickImportFile,
  saveLink,
  type LocalLinkRecord,
} from './localLink'
import { mirrorToFolder } from './localLinkMirror'

const MIRROR_DEBOUNCE_MS = 1_500

/** F9:不支持 File System Access → 只保留导出/导入(F1/F2),不暴露目录关联。 */
export type LocalLinkStatus = 'unsupported' | 'idle' | 'needs-permission' | 'linked'

export type LocalLinkState = {
  status: LocalLinkStatus
  /** 关联目录名(F8 展示);未关联为 null。 */
  dirName: string | null
  /** 上次全量/增量备份完成时间(ms);从未备份为 null。 */
  lastBackupAt: number | null
  /** 镜像 / 导入进行中(F8 状态)。 */
  busy: boolean
  /** 上次动作错误信息(F8 降级提示);正常为 null。 */
  error: string | null
}

export type LocalLinkActions = {
  link: () => void
  unlink: () => void
  backupNow: () => void
  reconnect: () => void
  importFolder: () => void
}

export type UseLocalLinkArgs = {
  projectId: string | null
  projectGeneration: number
  documentVersion: number | undefined
  documentReady: boolean
  /** 复用 App 既有 manifest.import(File)提案链:上传→preview→提案卡。 */
  onImportFile: (file: File) => void
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  if (error && typeof error === 'object' && 'message' in error) {
    return String((error as { message: unknown }).message)
  }
  return String(error)
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

export function useLocalLink(args: UseLocalLinkArgs): LocalLinkState & LocalLinkActions {
  const { projectId, projectGeneration, documentVersion, documentReady, onImportFile } = args
  const supported = localLinkSupported()

  const [status, setStatus] = useState<LocalLinkStatus>(supported ? 'idle' : 'unsupported')
  const [dirName, setDirName] = useState<string | null>(null)
  const [lastBackupAt, setLastBackupAt] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const recordRef = useRef<LocalLinkRecord | null>(null)
  const mirroringRef = useRef(false)
  const mirrorPendingRef = useRef(false)
  // 项目不变时 unlink/relink 也必须让旧异步镜像失效，不能把已删除的句柄写回 IndexedDB。
  const linkEpochRef = useRef(0)
  // 项目切换后,in-flight 的异步动作不得把结果写到新项目上。
  const tokenRef = useRef('')
  const token = projectId ? `${projectId}:${projectGeneration}` : ''
  tokenRef.current = token
  const onImportFileRef = useRef(onImportFile)
  onImportFileRef.current = onImportFile

  const resetLocal = useCallback(() => {
    linkEpochRef.current += 1
    recordRef.current = null
    mirrorPendingRef.current = false
    setDirName(null)
    setLastBackupAt(null)
    setBusy(false)
    setError(null)
    setStatus(supported ? 'idle' : 'unsupported')
  }, [supported])

  // 项目切换:重置并尝试恢复该项目已保存的句柄(F5;仅 query,不 request)。
  useEffect(() => {
    resetLocal()
    if (!supported || !projectId) return
    let cancelled = false
    const loadToken = token
    void (async () => {
      try {
        const record = await loadLink(projectId)
        if (cancelled || tokenRef.current !== loadToken || !record) return
        recordRef.current = record
        setDirName(record.handle.name)
        setLastBackupAt(record.lastBackupAt)
        const granted = await ensurePermission(record.handle, { request: false })
        if (cancelled || tokenRef.current !== loadToken) return
        setStatus(granted ? 'linked' : 'needs-permission')
      } catch {
        // 恢复失败不阻断工具主流程:停在 idle,用户可重新关联。
      }
    })()
    return () => { cancelled = true }
  }, [projectId, supported, resetLocal, token])

  const runMirror = useCallback(async (): Promise<void> => {
    let record = recordRef.current
    if (!record || !projectId || projectGeneration <= 0) return
    if (mirroringRef.current) {
      mirrorPendingRef.current = true
      return
    }
    mirroringRef.current = true
    const runToken = tokenRef.current
    const runLinkEpoch = linkEpochRef.current
    setBusy(true)
    setError(null)
    try {
      do {
        mirrorPendingRef.current = false
        await mirrorToFolder(record, projectId, projectGeneration)
        if (tokenRef.current !== runToken || linkEpochRef.current !== runLinkEpoch) return
        const now = Date.now()
        const next: LocalLinkRecord = { handle: record.handle, lastBackupAt: now }
        // 句柄与完成时间要作为同一成功结果持久化；失败时不能在 UI 冒充已备份。
        await saveLink(projectId, next)
        if (tokenRef.current !== runToken || linkEpochRef.current !== runLinkEpoch) return
        record = next
        recordRef.current = next
        setLastBackupAt(now)
      } while (mirrorPendingRef.current)
    } catch (e) {
      if (tokenRef.current === runToken && linkEpochRef.current === runLinkEpoch) {
        setError(messageOf(e))
      }
    } finally {
      mirroringRef.current = false
      if (tokenRef.current === runToken && linkEpochRef.current === runLinkEpoch) {
        setBusy(false)
      }
    }
  }, [projectId, projectGeneration])

  // F4:文档版本变化(SSE 驱动)→ 防抖 → 单向增量镜像。仅在已授权关联态触发。
  useEffect(() => {
    if (status !== 'linked' || !documentReady || projectGeneration <= 0) return
    const timer = setTimeout(() => { void runMirror() }, MIRROR_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [status, documentReady, projectGeneration, documentVersion, runMirror])

  const link = useCallback(() => {
    if (!supported || !projectId) return
    const runToken = tokenRef.current
    void (async () => {
      try {
        setError(null)
        const handle = await pickDirectory()
        if (tokenRef.current !== runToken) return
        const granted = await ensurePermission(handle, { request: true })
        if (tokenRef.current !== runToken) return
        if (!granted) { setError('permission_denied'); return }
        const record: LocalLinkRecord = { handle, lastBackupAt: null }
        await saveLink(projectId, record)
        if (tokenRef.current !== runToken) return
        linkEpochRef.current += 1
        recordRef.current = record
        setDirName(handle.name)
        setLastBackupAt(null)
        setStatus('linked')
        await runMirror()
      } catch (e) {
        if (isAbort(e) || tokenRef.current !== runToken) return
        setError(messageOf(e))
      }
    })()
  }, [supported, projectId, runMirror])

  const unlink = useCallback(() => {
    if (!projectId) return
    const pid = projectId
    const record = recordRef.current
    if (!record) return
    // 先使所有进行中的镜像失效；即使底层写文件已经开始，也不得在 unlink 后重建句柄记录。
    linkEpochRef.current += 1
    mirrorPendingRef.current = false
    recordRef.current = null
    setBusy(true)
    setError(null)
    void (async () => {
      try {
        await deleteLink(pid)
      } catch (e) {
        if (!tokenRef.current.startsWith(`${pid}:`)) return
        // 删除持久句柄失败时保持真实关联态，避免 UI 显示已解除、刷新后却重新出现。
        linkEpochRef.current += 1
        recordRef.current = record
        setDirName(record.handle.name)
        setLastBackupAt(record.lastBackupAt)
        setStatus('linked')
        setBusy(false)
        setError(messageOf(e))
        return
      }
      if (tokenRef.current.startsWith(`${pid}:`)) resetLocal()
    })()
  }, [projectId, resetLocal])

  const reconnect = useCallback(() => {
    const record = recordRef.current
    if (!record || !projectId) return
    const runToken = tokenRef.current
    void (async () => {
      const granted = await ensurePermission(record.handle, { request: true })
      if (tokenRef.current !== runToken) return
      if (!granted) { setError('permission_denied'); return }
      setStatus('linked')
      await runMirror()
    })()
  }, [projectId, runMirror])

  const backupNow = useCallback(() => {
    const record = recordRef.current
    if (!record || !projectId) return
    const runToken = tokenRef.current
    void (async () => {
      const granted = await ensurePermission(record.handle, { request: true })
      if (tokenRef.current !== runToken) return
      if (!granted) { setError('permission_denied'); setStatus('needs-permission'); return }
      setStatus('linked')
      await runMirror()
    })()
  }, [projectId, runMirror])

  const importFolder = useCallback(() => {
    const record = recordRef.current
    if (!record || !projectId) return
    const runToken = tokenRef.current
    void (async () => {
      try {
        setError(null)
        const granted = await ensurePermission(record.handle, { request: true })
        if (tokenRef.current !== runToken) return
        if (!granted) { setError('permission_denied'); setStatus('needs-permission'); return }
        setStatus('linked')
        const file = await pickImportFile(record.handle)
        if (tokenRef.current !== runToken || !file) return
        onImportFileRef.current(file)
      } catch (e) {
        if (isAbort(e) || tokenRef.current !== runToken) return
        setError(messageOf(e))
      }
    })()
  }, [projectId])

  return {
    status, dirName, lastBackupAt, busy, error,
    link, unlink, backupNow, reconnect, importFolder,
  }
}
