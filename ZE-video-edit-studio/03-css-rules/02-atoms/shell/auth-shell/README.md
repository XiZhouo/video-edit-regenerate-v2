# auth-shell

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

auth-shell.css — ze-login/ze-project-picker/ze-share 账号面外壳组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:登录 gate(.ze-login*)/ 项目选择器(.ze-project-picker*)/ 共享对话框
(.ze-share-*)是框架级账号面 shell chrome(LoginPage / ProjectPicker / ShareButton,
01-core/04-user-facing/03-authentication 与 header/controls)。含状态化样式 —— 项目行

## 对外表面

`.ze-login`, `.ze-login__card`, `.ze-login__error`, `.ze-login__hint`, `.ze-login__secondary`, `.ze-login__switch`, `.ze-login__title`, `.ze-project-picker`, `.ze-project-picker__demo`, `.ze-project-picker__demo-desc`, `.ze-project-picker__demo-list`, `.ze-project-picker__demo-name`, `.ze-project-picker__demo-scene`, `.ze-project-picker__demos`, `.ze-project-picker__error`, `.ze-project-picker__hint`, `.ze-project-picker__list`, `.ze-project-picker__new`, `.ze-project-picker__row`, `.ze-project-picker__row-meta`, `.ze-project-picker__row-name`, `.ze-project-picker__row-open`, `.ze-project-picker__section-label`, `.ze-project-picker__title`, `.ze-share-card`, `.ze-share-card__add-row`
