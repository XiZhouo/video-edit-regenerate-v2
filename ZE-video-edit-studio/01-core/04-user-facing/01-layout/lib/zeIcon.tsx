/**
 * zeIcon — 令牌图标渲染件(零依赖叶子;header / footer / editor zones / 装配层共用)
 *
 * 图标 = 令牌化图库(02-atoms/icons.md 单一真源):`<span class="ze-icon" data-icon=<name>>`,
 * 视觉全在皮肤(01-tokens/icons.css 令牌 + 02-atoms/icon-map.css mask 映射),本件零样式。
 * 名册来自生成物 icons.json(icon_tokens_check 门禁守同步);图标永远 aria-hidden,
 * 可达性语义放宿主按钮的 aria-label/title。
 *
 * IconOrGlyph = 注入轴双轨(ToolbarAction.icon / CanvasToolDef.icon 等「字符以数据流入」的
 * 合同接缝,P-4 兼容收编):值命中图库名 → token 图标;否则按字符直显(旧 glyph 注入零破坏)。
 */

import type { ReactNode } from 'react'
import iconManifest from '@ze/css/02-atoms/media/icons/icons.json'

const ICON_NAMES: ReadonlySet<string> = new Set(iconManifest.names)

export type ZeIconSize = 'xs' | 'sm' | 'lg'
export type ZeIconTone = 'muted' | 'accent' | 'success' | 'warn' | 'danger' | 'info'

export type ZeIconProps = {
  name: string
  size?: ZeIconSize
  tone?: ZeIconTone
  spin?: boolean
}

export function ZeIcon({ name, size, tone, spin }: ZeIconProps): ReactNode {
  return (
    <span
      className="ze-icon"
      data-icon={name}
      data-size={size}
      data-tone={tone}
      data-spin={spin ? 'true' : undefined}
      aria-hidden="true"
    />
  )
}

/** 值是否图库名(icons.json 名册;非名册值走字符直显)。 */
export function isIconName(value: string): boolean {
  return ICON_NAMES.has(value)
}

/** 注入轴双轨渲染:图库名 → ZeIcon;其余(旧 unicode glyph / 任意短字符)直显。 */
export function IconOrGlyph({ value, size }: { value: string; size?: ZeIconSize }): ReactNode {
  if (isIconName(value)) return <ZeIcon name={value} size={size} />
  return <span aria-hidden="true">{value}</span>
}
