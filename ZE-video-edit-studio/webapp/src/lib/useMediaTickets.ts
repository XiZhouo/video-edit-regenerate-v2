import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  assetUrl,
  requestAssetTickets,
  type AssetRefDict,
} from './api'

const SHA256 = /^[0-9a-f]{64}$/
const BATCH_SIZE = 256
const REFRESH_AHEAD_MS = 15_000

/** Keep positive and authoritative-negative batches on the same quiet cadence. */
export function mediaTicketRefreshDelayMs(expiresAt: number, nowMs = Date.now()): number {
  return Math.max(1_000, expiresAt * 1000 - nowMs - REFRESH_AHEAD_MS)
}

/** Mirror the framework asset-ref walk without trusting arbitrary sha-shaped strings. */
export function collectAssetSha256s(value: unknown): string[] {
  const found = new Set<string>()
  const seen = new WeakSet<object>()
  const walk = (item: unknown) => {
    if (!item || typeof item !== 'object') return
    if (seen.has(item)) return
    seen.add(item)
    if (Array.isArray(item)) {
      for (const child of item) walk(child)
      return
    }
    const record = item as Record<string, unknown>
    if (typeof record.sha256 === 'string' && SHA256.test(record.sha256)) {
      found.add(record.sha256)
    }
    for (const child of Object.values(record)) walk(child)
  }
  walk(value)
  return [...found].sort()
}

type TicketState = {
  namespace: string
  tickets: Record<string, string>
  expiresAt: number
}

/**
 * Keep native-media capabilities fresh without ever putting the login token in
 * a URL. Missing/failed tickets render an empty source; there is deliberately
 * no cookie or session-query fallback hidden in this resolver.
 */
export function useMediaTickets(
  projectId: string | null,
  generation: number,
  assetSources: unknown,
): (ref: AssetRefDict) => string {
  const shas = useMemo(() => collectAssetSha256s(assetSources), [assetSources])
  const shaKey = shas.join(',')
  const namespace = projectId && Number.isSafeInteger(generation) && generation > 0
    ? `${projectId}\0${generation}`
    : ''
  const [state, setState] = useState<TicketState>({ namespace: '', tickets: {}, expiresAt: 0 })

  useEffect(() => {
    const controller = new AbortController()
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    setState({ namespace, tickets: {}, expiresAt: 0 })
    if (!namespace || !projectId || shas.length === 0) {
      return () => controller.abort()
    }

    const refresh = async () => {
      try {
        const tickets: Record<string, string> = {}
        let expiresAt = Number.MAX_SAFE_INTEGER
        for (let offset = 0; offset < shas.length; offset += BATCH_SIZE) {
          const batch = await requestAssetTickets(
            projectId,
            generation,
            shas.slice(offset, offset + BATCH_SIZE),
            controller.signal,
          )
          Object.assign(tickets, batch.tickets)
          expiresAt = Math.min(expiresAt, batch.expires_at)
        }
        if (stopped) return
        if (expiresAt === Number.MAX_SAFE_INTEGER) expiresAt = 0
        setState({ namespace, tickets, expiresAt })
        const delay = mediaTicketRefreshDelayMs(expiresAt)
        timer = setTimeout(() => { void refresh() }, delay)
      } catch (error) {
        if (stopped || (error instanceof DOMException && error.name === 'AbortError')) return
        // A transient mint failure never broadens authority. Keep URLs blank and
        // retry briefly; normal document/auth error handling remains separate.
        setState({ namespace, tickets: {}, expiresAt: 0 })
        timer = setTimeout(() => { void refresh() }, 5_000)
      }
    }
    void refresh()
    return () => {
      stopped = true
      controller.abort()
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [generation, namespace, projectId, shaKey]) // shaKey is the stable content dependency

  return useCallback((ref: AssetRefDict) => {
    if (!namespace || state.namespace !== namespace || state.expiresAt <= Math.floor(Date.now() / 1000)) {
      return ''
    }
    const ticket = state.tickets[ref.sha256]
    return ticket ? assetUrl(ref, projectId!, generation, ticket) : ''
  }, [generation, namespace, projectId, state])
}
