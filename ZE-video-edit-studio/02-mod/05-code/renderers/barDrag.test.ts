/**
 * 拖条子的边界行为。这些约束全都只有拖到某个极限才暴露 —— 手动点点不全。
 *
 * 用例名写英文:i18n_contract_check 扫 02-mod/05-code 下所有 .ts 的字符串字面量,
 * 不认 .test.ts 这个例外(上游门禁的口子,不在本 skill 改受管脚本)。
 */
import { describe, expect, it } from 'vitest'
import { draggedSpan, snapCandidates, snapTime } from './barDrag'

/** 一层视频:时间线 2s 起、放 3s、取素材的 1s 处开始;素材总长 10s。 */
const clip = (dt: number, edge: 'body' | 'start' | 'end') => draggedSpan(
  { edge, start0: 2, dur0: 3, sourceIn0: 1, srcLen: 10, isClip: true, dt }, 20)

describe('video bar — move', () => {
  it('moving keeps duration and source in-point', () => {
    expect(clip(1.5, 'body')).toEqual({ start: 3.5, duration: 3, sourceIn: 1 })
  })

  it('cannot move before zero', () => {
    expect(clip(-99, 'body').start).toBe(0)
  })

  it('can move past the current end of the timeline — that just makes it longer', () => {
    // 标注条会被夹在总长内;视频条不该被,否则唯一一层永远拖不到后面去
    expect(clip(50, 'body').start).toBe(52)
  })
})

describe('video bar — trim the head (this is where it goes wrong quietly)', () => {
  it('trimming the head moves sourceIn by exactly the same amount', () => {
    // 掐掉开头 1 秒:起点 2→3,时长 3→2,源入点 1→2。
    // sourceIn 不跟着走的话,这一层放的还是素材第 1 秒起 —— 画面整个跳了 1 秒。
    expect(clip(1, 'start')).toEqual({ start: 3, duration: 2, sourceIn: 2 })
  })

  it('extending the head pulls sourceIn back, never below zero', () => {
    const got = clip(-5, 'start')          // 只有 1 秒可回退(sourceIn0=1)
    expect(got.sourceIn).toBe(0)
    expect(got.start).toBe(1)
    expect(got.duration).toBe(4)           // 起点 1、右缘仍钉在 5
  })

  it('start + duration always lands on the same frame of source', () => {
    for (const dt of [-3, -1, -0.4, 0.5, 1, 2.5]) {
      const g = clip(dt, 'start')
      // 右缘不动 → 时间线终点恒为 5;而终点对应的素材位置也必须恒定
      expect(g.start + g.duration).toBeCloseTo(5, 6)
      expect(g.sourceIn + g.duration).toBeCloseTo(4, 6)
    }
  })

  it('cannot squeeze below the schema floor', () => {
    expect(clip(99, 'start').duration).toBeCloseTo(0.2, 6)
  })
})

describe('video bar — trim the tail', () => {
  it('trimming the tail only changes duration', () => {
    expect(clip(-1, 'end')).toEqual({ start: 2, duration: 2, sourceIn: 1 })
  })

  it('cannot ask for more than the source has', () => {
    // 素材 10s、源入点 1s → 最多还能放 9s
    expect(clip(99, 'end').duration).toBe(9)
  })

  it('unknown source length means no cap — the domain will say so before compositing', () => {
    const g = draggedSpan({ edge: 'end', start0: 0, dur0: 2, sourceIn0: 0, srcLen: 0,
                            isClip: true, dt: 500 }, 20)
    expect(g.duration).toBe(502)
  })

  it('cannot squeeze below the schema floor', () => {
    expect(clip(-99, 'end').duration).toBeCloseTo(0.2, 6)
  })
})

describe('annotation bar keeps its old behaviour', () => {
  const mark = (dt: number, edge: 'body' | 'start' | 'end') => draggedSpan(
    { edge, start0: 2, dur0: 1, dt }, 10)

  it('single-frame annotations stay legal (duration may reach 0)', () => {
    expect(mark(-99, 'end').duration).toBe(0)
  })

  it('moving is clamped inside the timeline', () => {
    expect(mark(99, 'body').start).toBe(9)     // total 10 - dur 1
  })

  it('the tail is clamped to the end of the timeline', () => {
    expect(mark(99, 'end').duration).toBe(8)   // total 10 - start 2
  })

  it('annotations have no source in-point to keep in step', () => {
    expect(mark(0.5, 'start').sourceIn).toBe(0)
  })
})

describe('snapping', () => {
  it('snaps to a nearby gate', () => {
    expect(snapTime(2.02, [2, 5], 100)).toBe(2)          // 100px/s → 2px away
  })

  it('leaves it alone when nothing is close enough', () => {
    expect(snapTime(2.5, [2, 5], 100)).toBe(2.5)         // 50px away
  })

  it('tolerance is pixels, not seconds — zoomed in, the same gap no longer snaps', () => {
    expect(snapTime(2.04, [2], 100)).toBe(2)             // 4px
    expect(snapTime(2.04, [2], 1000)).toBe(2.04)         // 40px
  })

  it('picks the nearest gate when several are in range', () => {
    expect(snapTime(2.03, [2, 2.05], 200)).toBe(2.05)    // 6px vs 4px
  })

  it('a clip never snaps to its own edges', () => {
    const bars = [{ id: 'a', start: 0, duration: 2 }, { id: 'b', start: 3, duration: 1 }]
    const gates = snapCandidates(bars, 'a', 1.5, 4)
    expect(gates).toContain(3)          // b 的起点在
    expect(gates).toContain(1.5)        // 播放头在
    expect(gates.filter((g) => g === 2).length).toBe(1)   // 只剩整秒那个 2,不是 a 的终点
  })
})
