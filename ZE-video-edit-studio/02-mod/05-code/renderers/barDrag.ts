/**
 * barDrag — 时间线上拖条子的纯算式。
 *
 * 单独一个文件是为了能直接测:这里的每一条约束都是"不这么写就出人意料"的那种,
 * 而它们全都只有拖到某个边界才暴露 —— 靠手动点是点不全的。
 */

/** 拖条:整体平移 / 拉左端(钉右缘)/ 拉右端(钉左缘) */
export type Edge = 'body' | 'start' | 'end'

/** 拖条之后这一段在哪:body=整体平移,start=钉右缘,end=钉左缘。
 *
 *  视频条比标注条多两条约束,少一条都会出人意料:
 *
 *  ① **左缘裁剪必须同时改 sourceIn**。不改的话,掐掉开头 1 秒之后,这一层放的
 *     还是素材的第 0 秒起 —— 画面内容整个往后跳了 1 秒,而人只是想切掉头。
 *     sourceIn 往前走多少,时间线起点就往前走多少,两者恒等,画面才纹丝不动。
 *  ② **不能要素材没有的部分**。sourceIn ≥ 0(往左拉最多拉到素材真正的开头),
 *     sourceIn + duration ≤ 素材长度(往右拉最多拉到素材末尾)。越界的话合成时
 *     ffmpeg 会截出一个没有视频轨的碎片,再在拼接处以完全无关的报错炸掉。
 *
 *  ``srcLen <= 0`` = 不知道素材多长(生成层没有 asset),那就不设上限 ——
 *  真越界了域层会在合成前说人话(render.compose_span 的 probe 保护)。 */
export function draggedSpan(d: { edge: Edge; start0: number; dur0: number; dt: number
                          sourceIn0?: number; srcLen?: number; isClip?: boolean },
                     total: number): { start: number; duration: number; sourceIn: number } {
  const MIN = 0.2                       // clip.duration 的 schema 下限
  const inn0 = Math.max(0, d.sourceIn0 ?? 0)
  const srcLen = d.srcLen && d.srcLen > 0 ? d.srcLen : 0
  const min = d.isClip ? MIN : 0        // 标注允许 duration=0(单帧,合法)

  if (d.edge === 'body') {
    // 视频条不夹右边界:往后拖就是把时间线拉长,这是正常操作。
    const cap = d.isClip ? Number.MAX_SAFE_INTEGER : total - d.dur0
    const start = Math.max(0, Math.min(d.start0 + d.dt, cap))
    return { start, duration: d.dur0, sourceIn: inn0 }
  }

  if (d.edge === 'start') {
    const end = d.start0 + d.dur0
    // 往左最多拉到素材开头(sourceIn 归 0),往右最多留下 min
    const lo = d.isClip ? Math.max(0, d.start0 - inn0) : 0
    const start = Math.max(lo, Math.min(d.start0 + d.dt, end - min))
    // 只有视频层有源入点。标注没有素材可对齐,给它算一个位移过的 sourceIn 是个陷阱:
    // 眼下 onBarUp 不写它,但下一个调用方会照单全收。
    const sourceIn = d.isClip ? inn0 + (start - d.start0) : inn0
    return { start, duration: end - start, sourceIn }
  }

  const capByAsset = srcLen > 0 ? srcLen - inn0 : Number.MAX_SAFE_INTEGER
  const capByLine = d.isClip ? Number.MAX_SAFE_INTEGER : total - d.start0
  const duration = Math.max(min, Math.min(d.dur0 + d.dt, capByAsset, capByLine))
  return { start: d.start0, duration, sourceIn: inn0 }
}

/** 吸附:把时间贴到最近的一个「关口」上,前提是够近。
 *
 *  容差以**像素**给,不是秒 —— 缩放之后 0.1 秒可能是 2px 也可能是 200px,
 *  按秒给容差会让放大时吸得莫名其妙、缩小时完全吸不住。
 *
 *  没有够近的关口就原样返回。 */
export function snapTime(t: number, candidates: number[],
                         pxPerSec: number, tolPx = 6): number {
  if (!(pxPerSec > 0)) return t
  const tol = tolPx / pxPerSec
  let best = t, bestD = tol
  for (const c of candidates) {
    const d = Math.abs(c - t)
    if (d < bestD) { best = c; bestD = d }
  }
  return best
}

/** 吸附关口:整秒 + 别的条子的两端 + 播放头。自己那条不算(会把自己吸死在原地)。 */
export function snapCandidates(bars: { start: number; duration: number; id: string }[],
                               selfId: string, playhead: number, total: number): number[] {
  const out: number[] = [playhead]
  for (let s = 0; s <= Math.ceil(total); s += 1) out.push(s)
  for (const b of bars) {
    if (b.id === selfId) continue
    out.push(b.start, b.start + b.duration)
  }
  return out
}
