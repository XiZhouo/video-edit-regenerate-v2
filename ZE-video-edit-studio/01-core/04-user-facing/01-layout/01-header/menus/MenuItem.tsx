/**
 * MenuItem + MenuGroup — 菜单子项 / 分组
 *
 * 视觉:L2 .ze-menu-item / .ze-menu-group / .ze-menu-item__shortcut(03-css-rules/02-atoms/menu-item.css)
 *
 * 分组:菜单子项按"组"渲染,组与组之间自动出分割线(CSS adjacent sibling 选择器)。
 */

import { type ReactNode, type MouseEvent } from 'react'

export type MenuItemProps = {
  label: ReactNode
  /** 快捷键提示(显示在右侧,如 `Ctrl/Cmd+S`)*/
  shortcut?: string
  disabled?: boolean
  /** 危险动作走红色(SPEC §2.3.5.9 删除项目)*/
  variant?: 'default' | 'danger'
  /** 是否带 ▸ 子菜单标记 */
  hasSubmenu?: boolean
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void
}

export function MenuItem(props: MenuItemProps): ReactNode {
  return (
    <button
      type="button"
      className="ze-menu-item"
      role="menuitem"
      data-disabled={props.disabled ? 'true' : undefined}
      data-variant={props.variant ?? 'default'}
      data-has-submenu={props.hasSubmenu ? 'true' : undefined}
      onClick={props.disabled ? undefined : props.onClick}
      disabled={props.disabled}
    >
      <span>{props.label}</span>
      {props.shortcut && (
        <kbd className="ze-menu-item__shortcut">{props.shortcut}</kbd>
      )}
    </button>
  )
}

export type MenuGroupProps = {
  children: ReactNode
}

/**
 * MenuGroup — 包裹一组 MenuItem;组与组之间 CSS 自动出分割线
 */
export function MenuGroup(props: MenuGroupProps): ReactNode {
  return (
    <div className="ze-menu-group" role="group">
      {props.children}
    </div>
  )
}
