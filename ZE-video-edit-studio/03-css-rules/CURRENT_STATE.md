# 03-css-rules — 现状基线(CSS 根治后)

> 当前皮肤:**精密瓷釉 Precision Porcelain v4**(css 16.3.0,2026-06-12;基底晴空瓷釉 15.4.0)—— 词表只增不删的精密化重调(线条实色三档/玻璃更实/行高 26)+ 36 档工具主题色(`01-tokens/themes.css`,`data-ze-theme`);DNA 与跟随调整见 [SKIN-PORCELAIN.md](./SKIN-PORCELAIN.md)。以下治理基线(分层/门禁/结构)不变。

> 本文件是 CSS 体系的"治理后基线",供后续维护/换肤对照。生成于一次彻底根治(皮肤模型 + 内部结构)。

## 一句话
`03-css-rules/` = **唯一、自包含、可整体替换的"皮肤"**。复制本文件夹、改令牌即一套新皮肤;别处(webapp / 02-mod)只 `import` + 贴 `className`,不含 CSS 规则。

> **逻辑地图(引用 / 关联 / 类-vs-实例,全 5 层)**:见 [DESIGN-SYSTEM.md §2A](../DESIGN-SYSTEM.md)。想查某 CSS 对象谁在用、在哪登记、谁把关,查 §2A.2 关联表。

## 皮肤即文件夹(css = opt-in 同步层)
- 皮肤 = 整个 `03-css-rules/` 文件夹;**换肤 = 换文件夹**。
- css 是 `skill_sync` 的 **opt-in 同步层**(`--layers css`;每 skill 有 `02-mod/_locks/css.lock.json` 记 `_contract.json` 的 interface_version+checksum)。**默认不随 core 自动动**、归各 skill 自有;但 core 前端长出新类时经 `--layers css` 或 core 联动(css coupling)下发,**目标皮肤含 `html[data-mod]` 定制块则不自动带**(防覆盖定制)。删/改令牌词表 = MAJOR = 下游 incompat,需按 SOP 手动过闸。
- 无 `02-mod/07-theme.css`(已废)。每 skill 改色 = 改自己皮肤的 `01-tokens/tokens.css`(scaffold 可选注入 `[data-mod]` 覆盖块)。
- 校验:`css_contract_check.py`(className↔词表自洽)仍在,与 sync 无关。

## 五层(目录号即层级,无 L1–L7 双轨)
| 目录 | 职责 | 关键文件 |
|---|---|---|
| `01-tokens/` | 令牌字典(`--ze-*`,light + `[data-theme="dark"]`),含 lifecycle state token | `design/tokens/code.css` / `theme/tool-themes/code.css` |
| `02-atoms/` | `.ze-*` UI 对象库(唯一真相,上层只复用不自造) | `controls/button/code.css` / `data/thumb/code.css` / `feedback/state-frame/code.css` 等 |
| `03-zones/` | 编辑器内部功能区(操作区+内容区;zone = 固定骨架 + 受约束拓展点) | `editor/inspector/code.css` / `navigation/outliner/code.css` |
| `04-editors/` | 单个编辑器(只编排功能区和内容结构) | `input/form/code.css` / `workflow/pipeline/code.css` / `media/scene3d/code.css` |
| `05-compositions/` | 编辑器组合层(Header/Footer/Layout/Auth/App shell) | `navigation/header/code.css` / `layout/layout/code.css` |

> "定位 / 动画 / 交互时机" 不是 CSS 层 —— 由 React 运行时处理(原 "L7" 概念已并入此说明,无空目录)。

## 治理后的硬规矩
1. **区域不自造原子**:footer/auth/editor 区域只做布局;按钮/输入/进度条/tabs/kv-list/chip/卡片一律用 `02-atoms`(如 `.ze-progress` / `.ze-icon-button[data-size="xs"]` / `.ze-tabs[data-density="compact"]` / `className="ze-card ze-login__card"`)。
2. **一物一名**:图标按钮只有 `.ze-icon-button`;容器只有 `.ze-header`;变体走 `data-variant`(不用 BEM `--`)。
3. **令牌唯一来源**:所有视觉值 `var(--ze-*)`,禁字面量(几何字面量如 14/18px 图标有文档化豁免)。
4. **诚实**:zone/编辑器视觉的接线状态按实情标注(gallery 各节点卡的状态徽章与机器推导的引用行即真源)——不写成"已交付/已接线"。
5. **一对象一文件夹**:代码在 `code.css`;说明、规则、校验、扩展规则与代码同目录。根层级只保留 public entry。
6. **状态从 token 起步**:normal/empty/running/error/stale/disabled 先在 L1 定义,再由 L2 UI 对象消费;上层只把 entity/run 状态映射到 `data-ui-state` / `data-run-phase`。

## 总览页
合并进**交互式 gallery**(`webapp/gallery.html`,React;`cd webapp && npm run dev` → `localhost:5173/gallery.html`)—— 单页 L1 令牌→L2 原子→L3 功能区→L4 编辑器→L5 组合,每层带配套规则卡,L4 是真编辑器(可交互),顶部 light/dark + **36 档工具主题色切换**(`data-ze-theme`)实时换肤驱动全页。换肤冒烟:切主题档看令牌/原子/区/编辑器/组合整体观感联动。
