# CSS 模块设计哲学

CSS 在 build v3 里不是“样式补丁堆”，而是一棵可消费、可校验、可替换的对象树。每一层只做自己该做的事，文件夹结构必须让人一眼看出：这个对象的代码在哪、规则是什么、怎么校验、以后怎么扩展。

## 五层

1. `01-tokens`：系统语言。颜色、字体、间距、尺寸、圆角、动效、状态都先在这里命名。状态 token 也在这里，不在 UI 层临时配色。
2. `02-atoms`：UI 对象。按钮、字段行、缩略图、表格、菜单项、编辑器 shell 等都在这里定义自己的外观和状态分支。
3. `03-zones`：功能区。它只安排编辑器内部的操作区、属性区、大纲区、状态条等结构，不重新画按钮、卡片、行。
4. `04-editors`：单个编辑器。表格、表单、画布、时间轴、管线等编辑器只编排自己的功能区和内容结构。
5. `05-compositions`：编辑器组合层。工作台壳、Header、Footer、登录/项目选择、LayoutTree 等在这里，只组合编辑器和 shell。

功能区是**有组织的职责区域**,不是默认多套一层视觉壳。一个 editor 可以包含多个相关功能区(摘要、筛选、列表、日志、工具、状态等),只要它们服务同一个工作闭环,就应该在同一个 editor 内按 zone 组织;不要把每个功能区拆成一个 editor,也不要用 panel/unit/card 外框证明"它是一个区域"。

## 文件夹规则

对象必须是文件夹。一个对象文件夹至少有：

- `code.css`：真实 CSS。
- `README.md`：这个对象是什么，暴露哪些 class/data。
- `RULES.md`：这个对象不能怎么用。
- `VALIDATION.md`：改它要跑哪些校验。
- `EXTENSION.md`：新增变体、状态、子元素时怎么做。

如果有分组，分组本身也是一层文件夹，并带 `README.md`。分组只负责收纳对象，不直接写业务样式。

## 消费规则

下层提供语言，上层消费语言。L2 可以用 L1 token；L3/L4/L5 不直接用 token 重新画视觉，而是组合 L2 对象和结构。

mod 消费 CSS 时只发三类事实：

- `className`：选对象，例如 `.ze-thumb`、`.ze-field-row`。
- `data-*` / ARIA：给对象状态和参数，例如 `data-ui-state="running"`、`data-size="sm"`。
- 运行时几何 CSS var：给尺寸、坐标、进度等数据，例如 `--split-basis`。

mod 不写共享 CSS，不内联视觉，不为某个场景临时造空态皮肤。

## 边界语法

功能区必须有边界,但边界不是一种到处复用的外框。Build v3 把边界当作语义:

- `chrome`: header/statusbar,只用单边线或透明,不块化。
- `control-group`: 默认操作行,外层不画同款四边块,由按钮、chip、segmented control 和组间分隔表达边界。
- `panel`: 少数需要独立面板感的区域,必须显式 `data-boundary="panel"`。
- `viewport`: canvas、preview、主工作面,必须有 surface、edge、radius、overflow,禁止裸奔。
- `rows`: list/table/outline,靠行分隔、选中态和滚动边界表达结构。
- `unit`: 属性/管线的信息控制单元,raised unit,内部再分 info/control。
- `pass-through`: 只在子级已有完整边界时使用,用于避免重复壳,不是去边界许可。

这条规则防两个极端:所有功能区硬套同款盒子,以及画布/主工作面没有边界。mod 只选择 zone 和拓展点,不私自画 border/radius/background。

默认组织方式优先级:先用职责分区、行分隔、控件组、滚动归属、选中态和间距建立层级;只有真正需要独立面板感时才显式选择 `panel`;只有 property/pipeline 的信息控制单元才使用 `unit`。列表、日志、检测结果和实体集合默认是 `rows` / card flow,不是一组 raised unit。

## 状态不是外挂组件

空、执行中、正常、异常、过期、禁用都是对象自身状态。对象没有内容时，它仍然是同一个对象，只是 `data-ui-state="empty"`；对象在跑任务时，它仍然是同一个对象，只是 `data-run-phase="running"`。

这条规则的目的很简单：刷新后可以精确到 entity 更新，列表里某一行失败就只让那一行进入 error，某个缩略图还在生成就只让那个缩略图进入 running。页面不做整页替换，也不因为一个对象的状态变化重排其它对象。

## 校验

核心校验：

- `python3 scripts/css_module_structure_check.py`
- `python3 scripts/css_layer_guard.py`
- `python3 scripts/css_container_contract_check.py`
- `python3 scripts/css_contract_check.py`
- `python3 scripts/mod_renderer_check.py`
- `python3 scripts/icon_tokens_check.py`

结构、层级、状态、图标生成物和 mod renderer 消费侧规则都要同时绿，CSS 才算真的能被后续 editor 和 mod 消费。
