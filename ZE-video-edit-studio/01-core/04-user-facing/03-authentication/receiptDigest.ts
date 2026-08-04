/**
 * §3.1 AI 设置中心「诊断与用量」页的纯逻辑(与 JSX 分离,vitest 可测)。
 *
 * 把 GET /api/ai/receipts 的 AiReceipt[](InvocationLog 脱敏读半)归纳成展示摘要:总计/成功率、
 * 按 provider·model·能力分组、错误码分布、延迟统计、最近一次实际落位。**诊断展示用,非计费权威**
 * (计费在 Commerce 侧另算)。纯函数,页面只按摘要渲染。
 */
import type { AiReceipt } from './aiOptions'

export type CountRow = { calls: number; ok: number; error: number }
export type ReceiptDigest = {
  total: number
  ok: number
  error: number
  /** 错误率 0..1(total=0 → 0)。 */
  errorRate: number
  byProvider: Array<{ provider: string } & CountRow>
  byModel: Array<{ model_id: string } & CountRow>
  byCapability: Array<{ capability: string } & CountRow>
  /** 错误码分布(status=error 的 error_code 计数),按计数降序、码名升序稳定。 */
  byErrorCode: Array<{ code: string; count: number }>
  latency: { count: number; avgMs: number; maxMs: number }
  /** 最近一次调用(created_at 最大);无记录 → null。`trace` 是该次的解析轨迹
   *  (§3.5 步8 / §6.2D「为什么换、换到哪」的展示源),后端未给则为空数组。 */
  mostRecent: {
    provider: string; model_id: string; status: string; created_at: number; trace: string[]
  } | null
}

function _bump(map: Map<string, CountRow>, key: string, ok: boolean): void {
  const row = map.get(key) ?? { calls: 0, ok: 0, error: 0 }
  row.calls += 1
  if (ok) row.ok += 1
  else row.error += 1
  map.set(key, row)
}

/** AiReceipt[] → 诊断摘要。纯函数;空/缺字段安全(按空串归类、错误率 0)。 */
export function buildReceiptDigest(receipts: AiReceipt[] | null | undefined): ReceiptDigest {
  const list = Array.isArray(receipts) ? receipts : []
  const byProvider = new Map<string, CountRow>()
  const byModel = new Map<string, CountRow>()
  const byCapability = new Map<string, CountRow>()
  const byErrorCode = new Map<string, number>()
  let ok = 0
  let error = 0
  let latCount = 0
  let latSum = 0
  let latMax = 0
  let mostRecent: ReceiptDigest['mostRecent'] = null

  for (const r of list) {
    const isOk = r.status === 'ok'
    if (isOk) ok += 1
    else {
      error += 1
      const code = r.error_code || 'unknown'
      byErrorCode.set(code, (byErrorCode.get(code) ?? 0) + 1)
    }
    _bump(byProvider, r.provider || '', isOk)
    _bump(byModel, r.model_id || '', isOk)
    _bump(byCapability, r.capability || '', isOk)
    if (typeof r.latency_ms === 'number' && Number.isFinite(r.latency_ms)) {
      latCount += 1
      latSum += r.latency_ms
      if (r.latency_ms > latMax) latMax = r.latency_ms
    }
    if (!mostRecent || r.created_at > mostRecent.created_at) {
      mostRecent = {
        provider: r.provider || '', model_id: r.model_id || '', status: r.status,
        created_at: r.created_at, trace: Array.isArray(r.trace) ? r.trace : [],
      }
    }
  }

  const total = list.length
  // 按 calls 降序、键名升序稳定排序(泛型 helper,各组用各自的键名字段名回填)。
  const sortRows = <T extends CountRow>(rows: T[], key: (r: T) => string): T[] =>
    rows.sort((a, b) => (b.calls - a.calls) || key(a).localeCompare(key(b)))
  const toRows = <K extends string>(m: Map<string, CountRow>, keyName: K) =>
    [...m.entries()].map(([k, v]) => ({ [keyName]: k, ...v }) as { [P in K]: string } & CountRow)

  return {
    total,
    ok,
    error,
    errorRate: total > 0 ? error / total : 0,
    byProvider: sortRows(toRows(byProvider, 'provider'), (r) => r.provider),
    byModel: sortRows(toRows(byModel, 'model_id'), (r) => r.model_id),
    byCapability: sortRows(toRows(byCapability, 'capability'), (r) => r.capability),
    byErrorCode: [...byErrorCode.entries()]
      .map(([code, count]) => ({ code, count }))
      .sort((a, b) => (b.count - a.count) || a.code.localeCompare(b.code)),
    latency: { count: latCount, avgMs: latCount ? Math.round(latSum / latCount) : 0, maxMs: latMax },
    mostRecent,
  }
}
