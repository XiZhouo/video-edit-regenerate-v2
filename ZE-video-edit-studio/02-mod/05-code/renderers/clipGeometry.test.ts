/**
 * 钉子表的前端一侧:`canvasRectOf` 算出来 == 域层算出来。
 *
 * 表由 Python 侧 `test_clip_geometry_fixture.py` 从 `_domain/clip_geometry.py` 生成。
 * 改任何一侧的算式,两条测试必红一条 —— 这就是"界面手柄"与"实际渲染"不许各走各的保证。
 */
import { describe, expect, it } from 'vitest'
import { canvasRectOf, isIdentity, normalizeGeometry } from './clipGeometry'
import fixture from './__fixtures__/clip_geometry_cases.json'

type Case = {
  name: string
  clip: Record<string, number | string>
  canvas: [number, number]
  src: [number, number]
  rect: { x: number; y: number; width: number; height: number }
}

// 用例名写英文:i18n_contract_check 扫 02-mod/05-code 下所有 .ts 的字符串字面量,
// 不认 .test.ts 这个例外(上游门禁的口子,不在本 skill 改受管脚本)。
describe('canvasRectOf matches the domain formula', () => {
  const cases = (fixture as { cases: Case[] }).cases

  it('fixture covers all four fit modes', () => {
    const modes = new Set(cases.map((c) => String(c.clip.fitMode ?? 'contain')))
    for (const want of ['contain', 'cover', 'stretch', 'none']) {
      expect(modes.has(want), `fixture is missing fitMode=${want}`).toBe(true)
    }
  })

  for (const c of cases) {
    it(c.name, () => {
      const got = canvasRectOf(c.clip as any, {
        width: c.canvas[0], height: c.canvas[1], srcW: c.src[0], srcH: c.src[1],
      })
      for (const key of ['x', 'y', 'width', 'height'] as const) {
        expect(got[key], `${c.name} / ${key}`).toBeCloseTo(c.rect[key], 3)
      }
    })
  }
})

describe('defaults and reset', () => {
  it('empty geometry is the identity placement', () => {
    expect(isIdentity({})).toBe(true)
    expect(isIdentity(undefined)).toBe(true)
  })

  it('any touched field breaks identity', () => {
    expect(isIdentity({ fitMode: 'cover' })).toBe(false)
    expect(isIdentity({ x: 0.1 })).toBe(false)
    expect(isIdentity({ scale: 1.2 })).toBe(false)
    expect(isIdentity({ cropB: 0.05 })).toBe(false)
  })

  it('scale has a floor — 0 would erase the picture', () => {
    expect(normalizeGeometry({ scale: 0 }).scale).toBe(0.01)
    expect(normalizeGeometry({ scale: -3 }).scale).toBe(0.01)
  })
})
