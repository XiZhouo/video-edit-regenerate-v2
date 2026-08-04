/**
 * LeftSegment — Header 左段
 *
 * SPEC: ./SPEC.md §2.3.1 + §2.3.2 skill 字标
 *
 * [header-v2] 左段 = 纯衬线品牌字标(--ze-font-display,唯一消费点):
 *   - FileMenu 退役(1.31.0):文件操作整体并入中段文件切换器(§2.3.5)。
 *   - .ze-header__icon 词表保留、CSS 默认隐藏;skillIcon prop 保留 API 兼容,本段不消费。
 *
 * `slot` 语义不变:mod 完全替换标识块(字标)。
 */

import { type ReactNode } from 'react'

export type LeftSegmentProps = {
  skillName: string
  /** 左段不渲染图标位(CSS .ze-header__icon 已隐藏);prop 保留兼容 mod 传参。 */
  skillIcon?: string
  slot?: ReactNode
  /**
   * 点击品牌字标 = 回「我的项目」首页(App 接 useProject().closeProject → 中区渲 ProjectPicker)。
   * 给了才把字标渲成可点按钮(第一触点:任何页都能一步回到新建/打开项目落地页,不必翻文件切换器菜单)。
   * 缺省 = 纯字标 span(向后兼容,模板态/未接 onHome 的 mod 不变)。
   */
  onHome?: () => void
  /** onHome 时的可访问名/tooltip(App 传 t('header.home');缺省回退品牌名)。 */
  homeHint?: string
}

export function LeftSegment(props: LeftSegmentProps): ReactNode {
  return (
    <div className="ze-header__left" data-ze-segment="left">
      {props.slot ?? (
        props.onHome ? (
          <button
            type="button"
            className="ze-header__skill-name"
            title={props.homeHint ?? props.skillName}
            aria-label={props.homeHint ?? props.skillName}
            onClick={props.onHome}
          >
            {props.skillName}
          </button>
        ) : (
          <span className="ze-header__skill-name" title={props.skillName}>
            {props.skillName}
          </span>
        )
      )}
    </div>
  )
}
