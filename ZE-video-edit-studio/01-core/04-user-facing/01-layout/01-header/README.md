# 01-header — Header

> 一句话：顶部栏区域。

样式排版 / 文件菜单 (打开/保存/新建) / 设置菜单 (主题/语言) / 帮助菜单 / 日夜间模式切换。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — 完整规则 + 技术文档 (待用户带着填)
- (代码文件待后续添加)

## 怎么用

Header 由框架 App.tsx 装配（`HeaderShell`），mod 不写 Header 代码，只提供数据：

- **左段产品名**：[`02-mod/04-layout.config.ts`](../../../../02-mod/04-layout.config.ts) 导出 `export const skillName = '我的工作台'`，App 传成 `config.skill_name`；中段项目名走 `doc.name`，两者不同源、别混用。
- **文件菜单**（新建/打开/最近项目/快照/重命名/删除）、**设置菜单**（主题/语言/数据目录）、**用户头像**：core 默认提供；主题/语言切换经 [05-preferences](../../05-preferences/) 的 `onThemeChange`/`onLocaleChange` 回调生效（不走 command），mod 零代码。
- **帮助菜单 → 操作手册**：`ManualDialog` 与项目选择页共用 `operationManual` 信任边界，加载 `config.manual_html ?? '/operation-manual.html'`（标准落点 `webapp/public/operation-manual.html`）。core 只保留语义/媒体标签与 `ze-om*` class，统一解析相对媒体 URL，并由 `.ze-operation-manual` 的 token 化 CSS 排版；片段中的脚本、事件、表单、SVG、嵌入页与全部私有 CSS 都会被移除。取不到安全片段时回退 `HeaderConfig.manual` 外链。
- 其余 `HeaderConfig` 键（`skill_icon` / `feedback_url` / `recent_max` / `snapshot_label_format`，见 [types.ts](./types.ts)）按需在装配处传入。

完整规则见 [SPEC.md](./SPEC.md)。
