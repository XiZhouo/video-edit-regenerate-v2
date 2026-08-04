# Visual Reference — Design Token 取值表

> **5 层体系 · L1 设计令牌 / Design Tokens** — 最底层,不引用任何层;只被 L2 直接消费。命名/层级/严格分层铁律见总纲 [DESIGN-SYSTEM.md](../../DESIGN-SYSTEM.md)。
>
> **适用规则**(本体见 DESIGN-SYSTEM §0.3 规则注册表):`ACCENT_HUE_HARDCODED`(accent 取值引色相种子)、`HARDCODED_FONT_SIZE`(本层定义 `--ze-fs-*` 角色字号令牌,上层禁字面字号)。响应式断点不是令牌、是 `BREAKPOINT_LITERAL` 钉死的 px 常量集(见下「响应式断点」)。

## 1. 这是什么

ZE 系列所有 skill 共享的**视觉变量字典** —— 颜色、字号、行高、间距、圆角、阴影、动效时长等设计原子的统一取值表。任何 ZE 工作台的 CSS / JS 都通过本叶子定义的 token 名引用视觉值,**不直接写字面量**。

业务白话视角:design token 等于"ZE 全系列共用的视觉调色板"。改一次 token 就改了所有 ZE skill 的同类视觉元素;反过来,看任何 ZE 工作台的颜色 / 字号 / 间距,都能在本叶子里找到对应 token 名。

token 不光是"取值",每个 token 都附**取值理由**(为什么是这个数 / 颜色,而不是另一个)—— 防止后续维护者为了凑设计稿乱改。

## 2. 边界与接口契约

### 2.1 跟其他叶子的关系

```
本目录 01-tokens 给出 token 字典
   ↓ 被引用(目录号即层级,无另一套 LX 编号)
02-atoms     (.ze-* 组件 CSS 用 token)
03-zones (zone 间距 / 默认色用 token)
04-editors (单个编辑器只组合 L2/L3,不直接用 token)
05-compositions (Header / Footer / Layout / App shell 等组合层只组合 L2/L4,不直接用 token)
React 交互层(运行时)(动效时长 / 反馈色用 token —— 非 CSS 目录,定位/动画在组件里)
```

本叶子**不引用**任何非前端叶子,纯视觉变量。

### 2.2 Token 分组(规定式:每档带角色;无"备而不用")

> 原则:清单即角色表。每个 token 回答"什么情境用",**没有"罗列一堆让人后续自选"的档**——0 引用档已删,新增档须带消费方或 `@deferred` 理由(门禁 `DEAD_*` 待 Phase 3)。取值见 `tokens.css`(每行注释即角色)。

| 分组 | 档 → 角色 |
|---|---|
| **color·surface** | canvas=工作台环境底 · **glass=贴边 chrome / 编辑器 shell 玻璃,配 --ze-glass-blur** · raised=面板内实底乳釉(卡片/行卡/菜单内胆) · float=内容浮层瓷盘(menu/popover/dialog/toast,实底) · sunken=input/画布/code(alpha 墨晕) · inverse=toast/媒体底 |
| **color·ink** | primary=正文/主标题 · secondary=标签 · tertiary=占位/计数/元数据 · disabled · inverse=反色面文字 |
| **color·accent** | 只留实际用档:200=浅边/浅块 · 500=主品牌 · 700=hover/按下。换色=改 `--ze-accent-h`(需要更多档随时按种子加,不预留 spare) |
| **color·semantic** | success/info/warn/danger — **仅状态语义**,勿当装饰色 |
| **typography·fs** | 2xs=微徽标/极小计数/装饰 glyph · xs=数字/角标/trace/微标签 · sm=**默认 UI 字号**:控件/表格/标签+正文/表单值 · lg=区块标题 · xl=对话框/页标题 · 2xl=超大展示(欢迎/空态) |
| **typography·lh** | tight=单行控件(文字容于行高) · snug=紧凑标签 · normal=多行正文 |
| **typography·fw** | regular=正文 · medium=标签/UI 强调 · semibold=区块标题 · bold=页主标题 |
| **space**(关系距离) | 0-5=2px 微距(图标贴合/细描边) · 1=图标↔文字 · 2=控件内距/紧凑组(最高频) · 3=行/卡内 · 4=section · 6=zone · 8/12/16=layout-only |
| **size**(命名尺寸) | `--ze-row-h`=26px **行/控件标准高(行高系统单位)** · field-label-w=112px · size-panel-w=240px(窄面板宽) |
| **scrollbar** | size=6px · track=transparent · thumb/hover=由 ink 派生的低对比可见条;所有可见滚动条统一 thin |
| **state**(生命周期) | normal / empty / running / error / stale / disabled 的 fg/bg/border 与不变尺寸;后端 entity/run 状态映射到 UI 自身 `data-ui-state` / `data-run-phase` |
| **radius** | sm=控件(5px) · md=卡片/弹层(8px) · lg=浮动面板(10px) · pill=徽章/HUD · none=满铺 |
| **shadow**(海拔) | flat · raised=hover/卡片 · **panel=常驻浮动面板(Header/Footer/编辑器)** · floating=popover/dropdown/浮窗 · overlay=dialog · **edge-highlight=顶缘高光(叠阴影栈最前)** · **glow-accent=accent 辉光(主按钮/选中/开关)**;dark 块重定义整组 |
| **glass** | glass-blur=10px · glass-saturate=1.1 — 与 surface-glass 严格配套的 backdrop-filter 档 |
| **motion** | dur fast=hover/状态/进入离开 · slow=大过渡 · **breathe=状态灯呼吸(2.4s)**;easing 两族:**easing-standard=snap 主缓动**(cubic-bezier(0.2,0,0,1)) · **spring=iOS overshoot 回弹**(cubic-bezier(0.34,1.42,0.5,1),精灵/入场/滑杆等回弹感;拍板#9 收编自 skill 叠加) |
| **z-index** | sticky<dropdown<modal<popover<tooltip<toast(谁压谁)|

**已删(0 引用,记入 MIGRATIONS 1.3.0)**:`fs-3xl`、`lh-relaxed`、`space-0`、`z-base`、`dur-instant`、`easing-decelerate/accelerate`、`--ze-bp-*`。`space-60` 重命名为 `--ze-size-panel-w`(它是尺寸不是间距)。

#### 响应式断点(不是令牌)

CSS `@container` 条件**无法**消费 `var()`(`@container (max-width: var(--x))` 非法)——所以断点不能是 token,曾经的 `--ze-bp-*` 永远是死码,已删。断点改为**被门禁 `BREAKPOINT_LITERAL` 钉死的 px 常量集**:

| 档 | 容器宽 | 边界 px |
|---|---|---|
| xs | <320 | 320 |
| sm | 320..480 | 480 |
| md | 480..768 | 768 |
| lg | 768..1024 | 1024 |
| xl | ≥1024 | 1440 |

`@container` 条件只许用 `{320,480,768,1024,1440}` 或其 ±16(滞后防抖)变体;其他裸值即红。

### 2.3 主题模式

ZE 默认提供 3 套主题:**light** / **dark** / **system**(跟随 OS)。

- 同一 token 在不同主题下取不同值(尤其颜色组);非颜色 token(间距 / 字号 / 动效)主题间相同
- 切换方式:`<html data-theme="light|dark|system">`;切换 = 替换 `:root` 下变量值,无需重渲染组件
- mod 可在 `02-mod/04-layout.md` 声明 default theme;不声明走 system

用户自定义主题(注入第 4 套 token 值)是 M3 范围,M1 不开放。

### 2.4 Token 落地形式

> **media/icon-tokens/code.css(生成文件)**:`--ze-icon-*` 图标令牌(data:image/svg+xml URI)由
> `scripts/icons_build.py` 从 `02-atoms/media/icons/icons.svg` 生成,**禁手改**;与 tokens.css/base.css
> 并列为 foundation 第三件(令牌字典 / 文档基线 / 图标令牌)。唯一消费点 = 02-atoms 的
> `media/icon-map/code.css`(mask-image),满足「foundation 之上只有 L2 消费令牌」铁律;皮肤可在
> `[data-mod]` 覆盖单个图标令牌换作品。规则与流程:`02-atoms/media/icons/icons.md`。


| 形式 | 用途 | 来源 |
|---|---|---|
| CSS 变量(`--ze-*`) | 主入口,所有 .ze-* 组件 CSS 直接引用 | 由本叶子的 token 对象定义 |
| JS 对象镜像(`Tokens.color.accent[500]`) | React/JS 代码读 token 值(尺寸计算、动态主题预览) | 由 `tokens.css` 自动生成 |
| Design 文件(可选) | Figma library / Sketch palette | 手动同步,不在本叶子范围 |

CSS 变量是**真实来源**,JS 镜像和 Design 文件都是镜像。

### 2.5 取值理由约定

每个 token 都附 `comment` 字段(在 `tokens.css` 里写 `/* 理由 */`),说明:
- 为什么是这个值(对比其他候选)
- 跟哪个上层判断绑定(如 `--ze-space-1 = 4px:因为 ZE 间距基准统一 4px,半档 0.5 = 2px 已经是最小可感间隔`)
- 不准空理由提交

### 2.6 不在本叶子范围

- 具体组件视觉(button 长什么样)→ 02-atoms
- 布局间距(zone 之间留多少 space)→ 02-atoms 布局对象;03-zones / 04-editors / 05-compositions 只组合
- 主题切换动画 → React 交互层(运行时)
- 用户自定义主题注入接口 → 皮肤即文件夹(换 03-css-rules 即换肤)

### 2.7 主题色(v4:36 档手工主题,每工具一档)

「**每个工具遵守同一套规范,只有主题色不同。**」每个 skill 的主题色 = `themes.css` 36 档手工设计主题中的**一个编号**(每 10° 一档;每档 light/dark × 4 设计值:500 主色 / 700 按下 / 200 浅块 / soft 大面积浅底);`wash / tint / focus-ring / glow` 在 tokens.css 用相对色语法由 accent-500 派生(`oklch(from var(--ze-accent-500) l c h / α)`),换档自动跟随。

> 历史:v2–v3 用 `--ze-accent-h` 单一色相种子公式推导;v4 改为逐档手工设计值——红区收明度、黄区提明度降彩度、蓝紫区提彩度,**公式推导给不出的跨色相对比一致性**(任意档主按钮反白对比 ≥4.5:1 由各档手工 L/C 锁定保证)。`--ze-accent-h/-c` 降为静态兼容别名(base.css 环境光斑仍引用),不再参与派生。

**声明方式(换主题色 = 声明一个档)**:
```html
<!-- 宿主 html;每个 skill 占一个编号 -->
<html data-ze-theme="22">
```

- **明暗成对**:每档 light/dark 各 4 值齐全(门禁强制),声明一次、自动贯穿明暗。
- **同相慎用档**:02 朱砂≈danger 26°、08 姜黄≈warn 78°、16 翠绿≈success 152°、23 钴蓝≈info 225° —— 重状态工具避开;同族工具建议取相邻档。
- 默认安装位(不声明)= tokens.css 出厂值(海蓝 252°,= 26 档附近)。
- 浏览器底线:`oklch(from …)` 需 Chrome 119+ / Safari 16.4+ / Firefox 128+(evergreen 2026 全支持)。

**机器门禁**:`scripts/css_layer_guard.py` 的 `ACCENT_HUE_HARDCODED` 规则(v4 语义)强制:accent 设计值(200/500/700/soft/tint)只准定义在 `tokens.css`/`themes.css`,且 themes.css 每档 light/dark×4 齐全 —— 把「换主题 = 换档、任意档不缺值」钉死,防散落覆盖与半套主题。

## 3. 情境应用指南(规定式:遇到场景查这里选 token)

不是"有这些值",而是"这个场景用这个组合"。新建/扩展视觉时按此选,别凭手感。

### 3.1 文本组合(字号 + 行高 + 字重)

| 场景 | 组合 |
|---|---|
| 按钮/控件内文字 | `fs-sm` + `fw-medium` + `lh-tight` |
| 表格单元/标签 | `fs-sm` + `fw-regular` + `lh-tight` |
| 表单值/卡片正文 | `fs-sm` + `fw-regular` + `lh-normal` |
| 区块/面板标题 | `fs-lg` + `fw-semibold` + `lh-snug` |
| 对话框/页面主标题 | `fs-xl` + `fw-bold` + `lh-snug` |
| 计数/角标/单位/trace | `fs-xs` + `ink-tertiary`(数值类加 `font-mono`)|
| 密集 chrome 微标签/计数胶囊/装饰 glyph | `fs-2xs`(badge sm / tab count / 折叠三角)|

**禁止**:区域/zone 文件写字面量 px 字号(门禁 `TYPOGRAPHY_FONT_SIZE_LITERAL`);为"显眼"把单行控件文字调到 ≥`fs-lg`;同一语义行内不同控件用不同字号。注:字号以 `fs-2xs/xs/sm/lg/xl/2xl` 六档收敛,`fs-sm` 为唯一默认。

### 3.2 间距(语义类别 × 场景密度,不是手感)

同组件内最小关系(图标↔文字)= `space-1`;控件内边距/紧凑组 = `space-2`;section 间 = `space-4`;zone 间 = `space-6`。`space-8/9/12/16` 只用于布局级(侧栏/整屏)。工作台外圈和分屏空气缝统一消费 `--ze-workspace-gutter`。

"行间距/卡"不是单一档——同一语义类别按**场景密度**取档:

| 场景 | 卡间/行间 | 卡内 |
|---|---|---|
| 单列卡片流(card-list / compact-list;卡自带边框即分隔) | `space-1` | `space-1` |
| 多列图墙(grid 缩略图) | `space-3` | `space-2` |
| 功能区行(zone 内控件组) | `space-1` | — |
| zone 之间 | `space-6` | — |

**别用大 gap 撑松散**——松散靠层级与分隔,不靠堆间距。反例:单列卡片流沿用图墙的 `space-3` = 用大 gap 撑松散(本节禁令的自我违反),一屏卡数直接减半。

### 3.3 颜色

文字按优先级选 ink(primary/secondary/tertiary);背景按海拔选 surface(canvas<sunken<raised);accent 只给"主操作/选中/重点";semantic 只给状态(别拿 danger 当红色装饰)。所有色走 `var(--ze-*)`,禁字面量(门禁 `LAYER_RAW_VISUAL`)。

### 3.4 海拔(shadow + z-index 配套)

浮起对象的阴影与层级要配套:hover/卡片 `shadow-raised`(无需 z)· dropdown/浮窗 `shadow-floating`+`z-dropdown` · popover/右键 `shadow-floating`+`z-popover` · dialog `shadow-overlay`+`z-modal` · tooltip `z-tooltip` · toast `z-toast`。

## 4. 行高系统(语义即容器 — 单一真源)

> 解决"语义同类的行(都是一个属性/一条记录)高度参差"。被各 editor CSS 与 manifest 共同遵守。

**规则**:
1. **行单位** = `--ze-row-h`(26px)。语义同类的单行单元(表单字段行、表格行、属性行、列表项)**统一**用 `min-height: var(--ze-row-h)`(表格单元用 `height: var(--ze-row-h)` 因 td 的 min-height 不可靠)。
2. **工作台 chrome 单行**:Header、Footer 折叠条 / 展开把手、Editor toolbar 都是 `1×--ze-row-h`;内层按钮/输入可用更小控件档,但不得撑高外栏。
3. **内层适配外层**:行内控件(input/select/checkbox/color/slider)高度 **≤ row-h 且垂直居中**,**不得撑高行**。文字用 `lh-tight` 保证容于行高。
4. **多行是显式例外**:textarea/JSON 等多行控件必须挂 `data-multiline`,高度 = `n × var(--ze-row-h)`(不是随手一个 px)。
5. **外层跟随**:行所在容器(`form-body`/`tbl-scroll`)只定滚动与内边距,不干预单行高度。
6. **舒适档**:个别编辑器(如 task 看板行)用更高行,走 `--ze-space-9`(36px)并在 manifest 声明 `rowModel`,门禁按声明校验,不一刀切。

**门禁**:每个在 manifest 声明 `rowModel: "row-h"` 的编辑器,其行/单元格选择器的高度必须来自 `--ze-row-h`(或整数倍);裸 px 即红(`editor_manifest_check.py`)。

## 11. 里程碑

- **M1** — token 分组规定式角色化 + 去 0 引用档 + 行高系统单一真源 + light/dark + 断点常量集化
- **M2** — JS 镜像 / Figma library 同步;§3 指南随新场景扩充
- **M3** — 用户自定义主题注入接口(mod 可覆盖某些 token)
