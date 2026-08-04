/**
 * MenuPopover — 通用菜单弹层容器
 *
 * 视觉:.ze-header__menu-popover(03-css-rules/04-editors/header.css §5)
 *
 * 用法:跟 usePopover hook 配合,父组件传 popoverRef + open 状态。
 */

import { type ReactNode, type RefObject } from 'react'

export type MenuPopoverProps = {
  open: boolean
  /** 弹层锚定方向(左 / 右 / 居中);center 供中段文件切换器(header-v2) */
  anchor: 'left' | 'right' | 'center'
  /** 加宽档(文件列表 / 成员面板等内容型弹层) */
  wide?: boolean
  popoverRef: RefObject<HTMLDivElement | null>
  ariaLabel: string
  children: ReactNode
}

export function MenuPopover(props: MenuPopoverProps): ReactNode {
  if (!props.open) return null

  return (
    <div
      ref={props.popoverRef}
      className="ze-header__menu-popover"
      data-anchor={props.anchor}
      data-wide={props.wide ? 'true' : undefined}
      role="menu"
      aria-label={props.ariaLabel}
    >
      {props.children}
    </div>
  )
}
