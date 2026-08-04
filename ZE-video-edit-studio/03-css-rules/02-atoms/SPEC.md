# UI Module — `.ze-*` 组件库 CSS 规则

> **5 层体系 · L2 UI 原子 / UI Atoms** — 唯一直接消费 L1 令牌的层(视觉原子 + 布局原子);被 L3 功能区组合。总纲(只引用下一层铁律)见 [DESIGN-SYSTEM.md](../../DESIGN-SYSTEM.md)。
>
> **适用规则**(本体见 DESIGN-SYSTEM §0.3 规则注册表):本层是 `.ze-*` 原子的**唯一定义层**(`LAYER_ATOM_REDEFINED`:别层只能组合、不能重定义,见 §1.1);颜色须令牌(`LAYER_RAW_VISUAL` 扩至本层);`@container` 断点须 canonical(`BREAKPOINT_LITERAL`);chrome 响应式一律 `@container` 非 `@media`(`VIEWPORT_MEDIA_FORBIDDEN`);窄栏纵向堆叠实现须在本层(`NARROW_STACK_REQUIRED`)。

## 1. 这是什么

ZE 系列所有 skill 共享的**基础组件 CSS 库** —— 按钮、输入框、卡片、弹层等"原子组件"的视觉规则。每个组件以 `.ze-<module>` 为 class 入口,内部样式全部由 [令牌](../01-tokens/SPEC.md) 推导,不写视觉字面量。

业务白话视角:UI 模块等于"ZE 工作台积木的最小颗粒"。所有 ZE skill 的按钮长得一样、所有输入框的 focus 反馈一致 —— 因为都用同一份 `.ze-*` 库。业务方写自己的 React/HTML 时只贴 class,不写 CSS。

### 1.1 原子定义权(规则 `LAYER_ATOM_REDEFINED`)

**`.ze-*` 原子只准在本层(02-atoms)定义。** L3 功能区 / L4 编辑器只能**组合**原子(摆位、加状态钩子),不准**重定义**原子的视觉。门禁 `LAYER_ATOM_REDEFINED`(见 DESIGN-SYSTEM §0.3 注册表)机械守此界。

- ✅ **定义(只在本层)**:`.ze-button { … var(--ze-*) … }` —— 单个 `.ze-X` 选择器、写视觉。
- ✅ **区域自有 block**(非原子):`.ze-editor { … }`、BEM element `.ze-editor__tbl { … }` —— 这些是 L4 自己的块,不是原子。
- ✅ **组合摆位**(带后代/子组合符):`.ze-editor .ze-button { margin-inline-start: … }` —— 只挪位置、不重绘。
- ❌ **重定义原子**:在 L3/L4 写 `.ze-button { background: … }` 单选择器改原子视觉 —— 违规;要改按钮长相,回本层改 `.ze-button`(单源),或加 `data-*` 变体。

判断口诀:**单个 `.ze-<已定义原子>` 选择器出现在 02-atoms 之外 = 重定义 = 红。** 需要差异化 → 用 `data-*` 属性变体(`.ze-button[data-variant=…]`),仍在本层定义。

## 2. 边界与接口契约

### 2.1 跟其他叶子的关系

```
01-tokens (token)
   ↓ 引用
本目录 02-atoms 把 token 实现成可贴 class 的视觉
   ↓ 被引用(目录号即层级)
03-zones (zone 内填的具体内容用 .ze-* 拼)
04-editors · header / footer (内部按钮、菜单都是 .ze-*)
04-editors · editor (editor 内 zone 内的 form/inputs/cards 都是 .ze-*)
React 交互层(运行时)(toast / dialog 的视觉用 .ze-toast / .ze-dialog)
```

### 2.2 模块清单 + 何时用 + 消费状态

> 不是"有一堆组件随便挑",而是"这个场景用这个"。**状态**列诚实标注真实消费(防"堆一堆没人用的"):✅=组件已消费 · ◐=低频/特定场景 · ⊘zone=暂只供 03 功能区用、无编辑器消费(styled, unwired) · ⊘defer=暂无场景(Phase 3 决定删/留,带理由)。选择矩阵见 §3。

| 模块 | 何时用 | 状态 |
|---|---|---|
| `ze-button` | 一切带文字的动作;变体见 §3.1 | ✅ |
| `ze-icon-button` | 纯图标动作(工具栏/关闭/行内操作)| ✅ |
| `ze-link` | 跳转/外链文字(不是动作按钮)| ◐ |
| `ze-disclosure` | 折叠组头(分组披露;≠按钮 ≠select,标题走 `.ze-text[data-role=title]`,见 §3.1 末)| ✅ |
| `ze-input` | 单行短文本/数字 | ✅ |
| `ze-textarea` | 多行文本/JSON(行高系统的多行例外)| ◐ |
| `ze-select` | 从已知选项选 1(枚举字段)| ✅ |
| `ze-checkbox` | 布尔开关(在表单字段里)| ◐ |
| `ze-radio` | 单选且需全部可见(<5 项)| ⊘defer |
| `ze-switch` | 即时生效的二值开关(配置行)| ◐ |
| `ze-slider` | 有界数值(min/max)调节 | ◐ |
| `ze-card` | 资产/缩略图/分组容器 | ◐ |
| `ze-divider` | 菜单/工具条分隔 | ⊘zone |
| `ze-tab` | 同区多视图切换 | ✅ |
| `ze-chip` | 可点/可删的小标签(筛选/工具)| ⊘zone |
| `ze-callout` | 持久内嵌提示(表单校验/区内说明)| ✅ |
| `ze-badge` | 静态小标记(计数/状态点)| ◐ |
| `ze-dialog` | 模态阻断确认 | ✅ |
| `ze-popover` | 锚定的上下文内容/菜单 | ◐ |
| `ze-tooltip` | hover 简短提示 | ⊘defer |
| `ze-toast` | 瞬时操作反馈(自动消失)| ⊘defer |
| `ze-progress` | 长流程进度条 | ✅ |
| `ze-skeleton`/`ze-spinner`/`ze-empty` | 加载/占位/空态(走 EntityLoadingFrame)| ✅ |
| `ze-status-tile` | 状态瓦片(任务态)| ◐ |
| `ze-kv-list` | 键值对只读列表(属性/详情)| ✅ |
| `ze-menu-item` | 菜单项(右键/下拉)| ◐ |
| `ze-stack`/`ze-row`/`ze-cluster` | 布局原子:竖排 / 横排 / 成组换行(`data-gap`/`data-align`/`data-justify`)| ✅ |
| `ze-grid` | 布局原子:等宽网格(`data-cols`=2/3/4/auto)| ✅ |
| `ze-bar` | 布局原子:顶/底功能条骨架(`data-edge`=top/bottom)| ✅ |
| `ze-header` / `ze-footer` / `ze-editor` shell | 工作台 chrome 外壳:Header、Footer 折叠条 / 展开把手、Editor toolbar 统一 `1×--ze-row-h`;只在本层消费 L1 行高令牌 | ✅ |
| `ze-spacer`/`ze-fill` | 布局原子:弹性占位 / 撑满剩余+可滚 | ✅ |
| `ze-scroll-fade-y/x/both` | 软滚动内容流边缘提示;卡片/表单/文本/日志/对话用,硬 viewport 不用 | ✅ |
| `ze-scroll` | 通用滚动归属点;优先用 `ze-fill`/真实内容流滚动盒,暂无直接消费场景 | ⊘defer |
| `ze-scrollbar` | 显式滚动容器的 thin scrollbar 视觉;已知 shell/scroll/fade 容器由 utility 自动覆盖,显式 class 暂无直接消费场景 | ⊘defer |
| `ze-sel` | 选中态唯一配方(19.9 美学批1;data-selected 已同配方,类形态供非 data 驱动件) | ⊘defer |
| `ze-card-hi` | raised 卡瓷釉顶缘高光(19.9;card atom 默认化随美学批2 收编) | ⊘defer |
| `ze-press-sm` | 图标按钮按压手感(19.9;icon-button 默认化随美学批2) | ⊘defer |
| `ze-lift` | ≥32px 行卡抬升手感(19.9;row-card 收编随美学批2) | ⊘defer |
| `ze-enter` | 面板错峰入场(19.9;编辑器空态/面板组接线随美学批2) | ⊘defer |
| `ze-enter-1` | 错峰 delay 阶梯(同上) | ⊘defer |
| `ze-enter-2` | 错峰 delay 阶梯(同上) | ⊘defer |
| `ze-enter-3` | 错峰 delay 阶梯(同上) | ⊘defer |
| `ze-enter-4` | 错峰 delay 阶梯(同上) | ⊘defer |
| `ze-springy` | 手操件弹簧(19.9;switch/slider 内建后此类给拖拽 ghost) | ⊘defer |
| `ze-breathe` | 进行中呼吸灯(19.9;status-tile 运行态收编随美学批2) | ⊘defer |
| `ze-text` | 角色文字:`data-role`=secondary/tertiary/title(裸标签 / 元数据 / 段标题)| ✅ |
| `ze-icon` | 令牌图标(`data-icon=<名>` × mask;美术源 `media/icons/icons.svg` 经 icons_build 生成 `--ze-icon-*` 令牌 + `media/icon-map/code.css`;`data-size`/`data-tone`/`data-spin`;语义映射/迭代规则单一真源 = `media/icons/icons.md`;门禁 icon_tokens_check)| ✅ |
| `state-frame` | UI 对象自身生命周期状态口径(`data-ui-state` / `data-run-phase` 映射 L1 `--ze-state-*`;不单独造空态组件)| ✅ |
| `ze-share-card` | 原 ShareButton 共享对话框卡片;header-v2(core 1.31.0)共享并入头像菜单(`.ze-header__acl-*`),失去消费 —— 词表保留待下次 MAJOR 清理 | ⊘defer |
| `ze-share-count` | 原 ShareButton 协作者计数;同上 header-v2 退役保留 | ⊘defer |
| `ze-share-overlay` | 原 ShareButton 遮罩层;同上 header-v2 退役保留 | ⊘defer |

> **布局原子族(layout.css)** 是 L2 第二族:视觉原子管「长什么样」,布局原子管「怎么排」。两族都只消费 L1 令牌,但布局原子把 `var(--ze-space-*)`(间距)/方向/对齐/网格收敛进来,**让 L3 功能区、L4 编辑器只组合布局原子排版,不再直接写裸令牌**(绝对严格分层地基,见 [DESIGN-SYSTEM §3](../../DESIGN-SYSTEM.md))。`data-gap` 取值 = 空间令牌档 key(0-5/1/2/3/4/6/8),省一层映射;新增枚举(方向/对齐/列数)只在 layout.css 加 `data-*` 闭集分支,上层禁为排版再写 `var()`。布局原子还带 `data-surface`(raised/sunken/canvas 容器底色);裸文字片段(次要标签 / 元数据 / 段标题)走 `.ze-text[data-role]`(text.css)。这两个实用原子让 L3/L4 去令牌后,容器底色与角色小字也有 L2 承接,不必自写 `var(背景/字号/墨色)`。

> ⊘defer 的 radio/tooltip/toast 当前无消费场景:radio→单选用 select;tooltip/toast 待真实交互需要(Phase 2 编辑器或 Phase 3)再接,**不预删也不假装在用**。Phase 3 据消费实情决定删/合并(门禁 `DEAD_ATOM`)。

### 2.2.1 视觉取舍准则(给 atoms 消费者)

这些准则用于决定"该用哪个原子 / 该不该包一层":

- **card 不是默认 section 壳**:`ze-card` 只用于独立重复项、资产缩略图、真正需要承载的详情块。若父区域只有一个子级且子级已经是视觉容器,父级应透明直通或合并。
- **细节不是噪音**:badge、chip、status dot、count、kv-list、row action 是编辑器信息密度的一部分。问题通常不是它们存在,而是边界、间距、对齐没有归类。
- **布局原子管排布,视觉原子管样式**:上层不要为了"稍微调一下"写私有 CSS;先看 `ze-stack/row/cluster/grid/bar/fill/scroll-fade` 是否已能表达。
- **浮层用实底,chrome 才 glass**:menu/popover/dialog/submenu 承载密集文字时必须偏 `surface-float`;不要把主 shell 的 glass 质感复制到内容浮层。
- **滚动原子按语义选**:内容流使用 `ze-scroll-fade-y/x/both`;画布、媒体、graph、timeline 这类硬 viewport 不使用 fade,由 viewport 边界负责裁切。`contentMode="pass-through"` 的复合主体不在父 content 上做 fade,应由内部真实内容流自行声明。功能区 `wrap-cluster` 只是控件排布,不默认顶部淡出。可见滚动条必须走 `--ze-scrollbar-*`/`.ze-scrollbar` 的 thin 语言,不要露浏览器默认粗条。

### 2.3 命名规范

- **base class**:`.ze-<module>`(单数,kebab-case)
- **变体**:`data-variant="primary|secondary|ghost|danger"` 属性,**不用 `--variant` BEM 修饰符**(原因:JS 切变体不用改 class string,只改一个 attr)
- **尺寸**:`data-size="sm|md|lg"`(全模块统一三档,`md` 默认)
- **状态**:`data-state` 或原生属性 — `:disabled` / `:focus-visible` / `:hover` / `[data-loading=true]` / `[data-active=true]`
- **嵌套元素**:`.ze-<module>__<part>`(BEM element 风格;只在嵌套时用,base 不写 `__base`)

例:
```html
<button class="ze-button" data-variant="primary" data-size="md" disabled>
  <span class="ze-button__icon">...</span>
  <span class="ze-button__label">Save</span>
</button>
```

### 2.4 状态变体集合(全模块统一)

| 状态 | 触发条件 | 表现约定 |
|---|---|---|
| default | — | 基础视觉 |
| hover | 鼠标悬停(`@media (hover:hover)`)| accent 提亮一档 |
| focus-visible | 键盘 focus | 显示 `--ze-focus-ring` 外环 |
| active | 鼠标按下 / 键盘 Enter 按住 | accent 压暗一档 |
| disabled | `:disabled` / `aria-disabled` | ink 降到 disabled 阶 + 取消 pointer |
| loading | `data-loading=true` | 显示 spinner + 阻断 pointer |

### 2.5 dark mode 策略

**靠令牌自动切换,不写独立 dark CSS**。

具体:`.ze-card` 里 background 永远写 `var(--ze-surface-raised)`,而非 `#fff`;`--ze-surface-raised` 在 light/dark 主题下取不同值。组件 CSS 完全主题无关。

例外:个别需要"反色对比"的场景(如 dialog overlay)可写 `light-dark()` 函数或单独主题选择器,但只能引用 token,不能写字面量。

### 2.6 跟其他叶子的接口

| 接口方向 | 接口点 |
|---|---|
| **输入** | 01-tokens token(所有视觉值来源)|
| **输出** | 给 03-zones / 04-editors 提供可贴 class 的组件;toast/dialog/popover/tooltip/progress 等反馈组件的视觉也在此 |
| **不提供** | React/Vue 组件 wrapper(本目录只产 CSS;wrapper 由 mod 自己写或上层组件提供) |

### 2.7 不在本目录范围

- 组件在 zone 里的位置 / 排布 → 03-zones
- 组件交互逻辑(点击 toast 关闭、dialog 打开关闭动画时机) → React 交互层(运行时)
- 业务自定义组件(canvas 上的浮动按钮、scene3d 的 gizmo 控件) → 各 editor 自己实现,不属于 `.ze-*`

## 3. 选择矩阵(遇到场景查这里选组件/变体,不凭手感)

### 3.1 按钮变体(`ze-button` / `ze-icon-button` 的 `data-variant`)

| 变体 | 用于 | 例 |
|---|---|---|
| `primary` | **主操作**(每屏≤1):提交/创建/保存 | "保存""生成" |
| `secondary` | 次要但重要(不需确认)| "预览""重置" |
| `ghost`(默认)| 工具栏/低权重动作 | 排序/搜索/工具按钮 |
| `danger` | **不可逆**:删除/停止 | 行内删、"停止任务" |

约束:文字说结果不说"确定"("保存为草稿"优于"确定");`icon-button` 默认 `data-size` 只用一档(别一屏混 sm/md/lg)。
成对决策(通过/驳回、采纳/弃用):肯定项 = `primary`,否定项 = `secondary` —— 否定项必须是可见按钮(禁 ghost 文字化);仅当动作不可逆(删除/停止)才用 `danger`,可逆的状态回写不算。(单卡 hover 浮层内的采纳按钮属单卡主操作,不计屏级 ≤1。)
折叠/披露头不是按钮变体:用 `.ze-disclosure`(子 `.ze-disclosure__caret` + `.ze-text[data-role=title]`),禁拿 ghost 按钮拉全宽充当。

### 3.2 输入控件(按数据形态选,别凭外观)

| 数据形态 | 控件 |
|---|---|
| 单行短文本/数字 | `ze-input` |
| 多行/JSON | `ze-textarea`(挂 `data-multiline`,行高系统例外)|
| 从已知选项选 1 | `ze-select` |
| 布尔(表单字段)| `ze-checkbox` |
| 即时生效二值开关 | `ze-switch` |
| 有界数值 | `ze-slider` |
| 单选且全可见 <5 项 | `ze-radio`(当前 defer,默认用 select)|

### 3.3 反馈组件分工(最易混,务必照此选)

| 组件 | 时机/形态 | 关键区别 |
|---|---|---|
| `ze-toast` | 瞬时操作反馈,**自动消失**,非阻断 | "已保存""导出完成" |
| `ze-dialog` | **模态阻断**,必须响应 | 删除确认/新建项目 |
| `ze-callout` | **持久内嵌**,跟随内容存在 | 表单校验错误/区内说明 |
| `ze-popover` | **锚定**某元素的上下文内容 | 菜单/字段帮助 |
| `ze-tooltip` | hover 简短提示 | 工具按钮含义 |

判据:要不要阻断?→ dialog。会不会自己消失?→ toast。是否常驻在内容里?→ callout。是否锚在某元素旁?→ popover/tooltip。

### 3.4 标记类区分

`ze-chip`=可点/可删(筛选、已选标签)· `ze-badge`=静态小标记(计数、状态点)· `ze-status-tile`=带语义色的状态块(任务态)。别用 badge 做可点筛选,也别用 chip 做纯展示计数。

### 3.5 chrome / 对话框内容怎么贴样式(视觉单一真相源)

总纲见 `../../DESIGN-SYSTEM.md` §0.1:**JSX 不内联视觉**,文字 / 键帽 / 键值表的视觉一律贴本层原子。对照表:

| 场景 | 贴什么 | 例 |
|---|---|---|
| 文字片段(标签 / 说明 / 计数 / 段标题) | `.ze-text` + `data-role`(secondary/tertiary/title)/ `data-size`(xs/sm)/ `data-weight`(medium/semibold/bold)/ `data-mono` | `<span class="ze-text" data-role="secondary" data-size="xs">标签</span>` |
| 只改墨色、不动字号 | `.ze-text` + `data-tone`(muted=三级墨 / secondary=次墨 / accent / danger / info / success / warn / inverse) | `<span class="ze-text" data-tone="muted">键</span>`(字号继承父行) |
| 键盘按键 / 快捷键键帽 | `.ze-kbd` | `<kbd class="ze-kbd">⌘↵</kbd>` |
| 键值表(label→value 行) | `.ze-kv-list` / `__row` / `__key` / `__value` | 见 `kv-list.css` |
| 富文本内联标记(`**bold**` / `<kbd>`) | 容器 `.ze-rich`(子 `<b>` 自动主墨、`<kbd>` 贴 `.ze-kbd`) | Trans.tsx |

**禁止**:在 `.tsx` 里 `style={{ color / fontSize / fontWeight / background / border … }}`(规则 8 `LAYER_INLINE_VISUAL` 拦)。结构性内联(布局 / 运行时定位尺寸)与 `style={{'--ze-x': v}}` 传 CSS var 放行。

### 3.6 如何扩展(后续 AI 照此做)

- **加新文字角色 / 墨色档**:在 `text.css` 加 `.ze-text[data-role=…]` 或 `[data-tone=…]` → 本节对照表补一行 → CSS 契约 **MINOR** bump(加性 class/属性)。
- **加新 chrome 原子**(继 `.ze-kbd` 之后,如 `.ze-tag-pill` 之类):新建 `02-atoms/<group>/<name>/code.css`,同目录补 `README.md` / `RULES.md` / `VALIDATION.md` / `EXTENSION.md` → 挂进 `02-atoms/index.css` `@import` → 本 SPEC §2.2 模块清单 + 本节补一行 → 契约 **MINOR**。
- **加新状态**:先在 `01-tokens/design/tokens/code.css` 补 `--ze-state-<name>-fg/bg/border`,再在 `feedback/state-frame/code.css` 做映射;业务只贴 `data-ui-state="<name>"`。
- **何时可内联**(不算违规):结构 / 布局(`display`/`flex`/`gap`/`left`/`width`/`transform`)、CSS 自定义属性传运行时值(`'--ze-x'`)、行尾 `/* layer-guard-allow-inline */`(极少数真·运行时视觉)。
- **改了已有 class/属性语义或删除** → 契约 **MAJOR**(见 `MIGRATIONS.md` 版本规则)。

## 4. 图标(统一映射,不散落)

icon = **令牌图库**(css 16.2.0 起):`<span class="ze-icon" data-icon="close" aria-hidden>` 在该用的地方直接引用,色随 currentColor。**禁止随手挑字符/随手画图**——语义映射与新增流程(画 symbol → icons_build → icons.md 登记 → MINOR)见 [`media/icons/icons.md`](./media/icons/icons.md)(单一真源);门禁 `icon_tokens_check`(图标R1-R3)守生成物同步/表实双向/名册。注入轴(ToolbarAction.icon / CanvasToolDef.icon)走 `lib/zeIcon.IconOrGlyph` 双轨,glyph 字符仍是合法降级。

## 11. 里程碑

- **M1** — 模块清单带"何时用"+ 消费状态 + 3.1–3.4 选择矩阵 + icon 映射;dark 靠令牌
- **M2** — 按需扩模块 + 视需要的 icon 组件化;React/Vue 轻 wrapper(可选)
- **M3** — A11y:✅ 可访问名基线烟测已落地([`03-editor/e2e_a11y.py`](../../01-core/04-user-facing/01-layout/03-editor/e2e_a11y.py):按钮/图片/控件可访问名 + 无正向 tabindex,全部 live 编辑器扫,0 缺陷);⏳ 完整 WCAG 2.1 AA(对比度、全键盘流、读屏流程)待续。据消费实情裁 defer 原子:✅ 检测已落地([`scripts/dead_atom_report.py`](../../scripts/dead_atom_report.py),只报告不删),裁剪由人定。
