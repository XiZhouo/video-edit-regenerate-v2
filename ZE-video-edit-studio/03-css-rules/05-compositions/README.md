# 05-compositions — 编辑器组合层

> L5 管多个编辑器如何组成一个工作台。它不定义按钮、卡片、字段行，也不写某个编辑器内部的业务画法。

## 对象

- `app/app-shell/code.css`：应用根壳和主区域结构。
- `access/auth/code.css`：登录、项目选择、共享入口的组合层结构。
- `navigation/header/code.css`：工作台顶部横栏挂载点。
- `status/footer/code.css`：工作台底部状态区挂载点。
- `layout/layout/code.css`：LayoutTree / slot / split 的组合层入口。

这些对象大多是 L2 shell 的消费者，真实 chrome 视觉在 `02-atoms/shell/*/code.css`。

## 消费逻辑

L5 只接收 layout 数据和 editor slot 声明。它可以决定“哪些编辑器放在哪个 pane”，不能替单个编辑器重画工具条，也不能替某个 entity 发明状态。entity 状态仍然落到对应 UI 对象自身。

## 校验

- `python3 scripts/css_module_structure_check.py`
- `python3 scripts/css_layer_guard.py`
- `python3 scripts/css_container_contract_check.py`
