#!/usr/bin/env node
/**
 * i18n_dump_keys.mjs — 把真 TS 词表的 key 集 dump 成 JSON(ground truth,给 i18n_sync / i18n_audit 用)。
 *
 * 不手解析 TS:用 esbuild(webapp/node_modules 已装)把 05-i18n/locales.ts + 02-mod/09-i18n.ts
 * bundle 成 ESM(strip types、解析相对 import),data-URL 动态 import 后读真值,再把嵌套词表
 * 展平成 dot-path key 集。
 *
 * 输出(stdout):
 *   {
 *     "locale_codes": ["zh","en"],
 *     "default_locale": "zh",
 *     "meta_codes": ["zh","en"],          // LOCALE_META 的 key(查注册表一致性)
 *     "framework": { "zh": [...dot-paths], "en": [...] },
 *     "mod":       { "zh": [...dot-paths], "en": [...] },
 *     "framework_values": { "zh": {key: value}, ... },  // 词表值(purity 门禁用)
 *     "mod_values":       { "zh": {key: value}, ... },
 *     "layout_slots": [{id, editor_class, nameKey, name}] | null,  // 04-layout.config 槽位(slot-name 门禁用;无布局=null)
 *     "layout_error": "..." | undefined   // 布局解析失败时给出原因(门禁按问题上报,不静默跳过)
 *   }
 *
 * 用法:node scripts/i18n_dump_keys.mjs [BUILD_ROOT]
 */
import { createRequire } from 'module'
import { fileURLToPath } from 'url'
import path from 'path'
import fs from 'fs'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const BUILD_ROOT = process.argv[2] ? path.resolve(process.argv[2]) : path.resolve(SCRIPT_DIR, '..')

// 从 webapp/node_modules 解析 esbuild(工具链层不自带依赖)。
const requireFromWebapp = createRequire(path.join(BUILD_ROOT, 'webapp', 'package.json'))
const esbuild = requireFromWebapp('esbuild')

/** 把一个 TS 入口 bundle 成 ESM 文本(strip types + 内联相对 import)。
 *  alias @ze/core → 01-core:layout.config 的 import type 会被 strip,但个别 mod 若引了
 *  运行时值也能解析(与 webapp vite 同一别名约定)。 */
async function bundleToEsm(entry) {
  const res = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    logLevel: 'silent',
    alias: { '@ze/core': path.join(BUILD_ROOT, '01-core') },
  })
  return res.outputFiles[0].text
}

/** 动态 import 一段 ESM 文本(data URL)。 */
async function importEsm(js) {
  const url = 'data:text/javascript;base64,' + Buffer.from(js, 'utf8').toString('base64')
  return import(url)
}

/** 把嵌套词表展平成 dot-path 集合(只收 string 叶子)。 */
function flatten(obj, prefix = '', out = []) {
  if (!obj || typeof obj !== 'object') return out
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string') out.push(key)
    else if (v && typeof v === 'object') flatten(v, key, out)
  }
  return out
}

/** 展平成 {dot-path: value}(purity 门禁要读真值)。 */
function flattenMap(obj, prefix = '', out = {}) {
  if (!obj || typeof obj !== 'object') return out
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string') out[key] = v
    else if (v && typeof v === 'object') flattenMap(v, key, out)
  }
  return out
}

/** 递归收 modLayout.tree 里的 slot 节点(slot-name 门禁用)。
 *  容器闭集 = split(first/second) + tabs(children[]),见 01-layout/types.ts LayoutNode。 */
function collectSlots(node, out = []) {
  if (!node || typeof node !== 'object') return out
  if (node.kind === 'slot') {
    out.push({
      id: node.id ?? '',
      editor_class: node.editor_class ?? '',
      nameKey: typeof node.nameKey === 'string' ? node.nameKey : null,
      name: typeof node.name === 'string' ? node.name : null,
    })
  } else if (node.kind === 'split') {
    collectSlots(node.first, out)
    collectSlots(node.second, out)
  } else if (node.kind === 'tabs') {
    for (const child of node.children ?? []) collectSlots(child, out)
  }
  return out
}

async function main() {
  const localesEntry = path.join(BUILD_ROOT, '05-i18n', 'locales.ts')
  const modEntry = path.join(BUILD_ROOT, '02-mod', '09-i18n.ts')
  const layoutEntry = path.join(BUILD_ROOT, '02-mod', '04-layout.config.ts')

  const localesMod = await importEsm(await bundleToEsm(localesEntry))
  const localeCodes = [...localesMod.LOCALE_CODES]
  const defaultLocale = localesMod.DEFAULT_LOCALE
  const meta = localesMod.LOCALE_META ?? {}
  const metaCodes = Object.keys(meta)

  const framework = {}
  const frameworkValues = {}
  for (const code of metaCodes) {
    framework[code] = flatten(meta[code]?.dict ?? {}).sort()
    frameworkValues[code] = flattenMap(meta[code]?.dict ?? {})
  }

  const mod = {}
  const modValues = {}
  if (fs.existsSync(modEntry)) {
    const modModule = await importEsm(await bundleToEsm(modEntry))
    const messages = modModule.messages ?? {}
    for (const code of Object.keys(messages)) {
      mod[code] = flatten(messages[code] ?? {}).sort()
      modValues[code] = flattenMap(messages[code] ?? {})
    }
  }

  // 布局槽位:无布局文件(cli/api-only)= null;解析失败给 layout_error(门禁上报,不静默)。
  // 两种合法形态:tree(显式布局树)或 preset+slots(预设,SlotSpec 映射,槽位 id=映射 key)。
  let layoutSlots = null
  let layoutError
  if (fs.existsSync(layoutEntry)) {
    try {
      const layoutMod = await importEsm(await bundleToEsm(layoutEntry))
      const ml = layoutMod.modLayout ?? {}
      // 优先级对齐运行时 loadLayout.ts:preset+slots 优先于 tree(两者都写时以 preset 为准)。
      if (ml.preset && ml.slots && typeof ml.slots === 'object') {
        layoutSlots = Object.entries(ml.slots).map(([id, spec]) => ({
          id,
          editor_class: spec?.editor ?? '',
          nameKey: typeof spec?.nameKey === 'string' ? spec.nameKey : null,
          name: null,
        }))
      } else if (ml.tree) {
        layoutSlots = collectSlots(ml.tree)
      } else if (ml.slots && typeof ml.slots === 'object') {
        layoutSlots = Object.entries(ml.slots).map(([id, spec]) => ({
          id,
          editor_class: spec?.editor ?? '',
          nameKey: typeof spec?.nameKey === 'string' ? spec.nameKey : null,
          name: null,
        }))
      } else {
        layoutSlots = []
        layoutError = 'modLayout 缺 tree 与 slots'
      }
    } catch (e) {
      layoutSlots = []
      layoutError = String(e && e.message ? e.message : e).slice(0, 400)
    }
  }

  process.stdout.write(JSON.stringify({
    locale_codes: localeCodes,
    default_locale: defaultLocale,
    meta_codes: metaCodes,
    framework,
    mod,
    framework_values: frameworkValues,
    mod_values: modValues,
    layout_slots: layoutSlots,
    ...(layoutError ? { layout_error: layoutError } : {}),
  }))
}

main().catch((e) => {
  process.stderr.write(`[i18n_dump_keys] ${e && e.stack ? e.stack : e}\n`)
  process.exit(2)
})
