# state-frame 校验

- `python3 scripts/css_module_structure_check.py` 必须看到 L1 state tokens 和本对象。
- `python3 scripts/css_layer_guard.py` 保证状态样式只用 token，不写一次性颜色。
- `python3 scripts/css_contract_check.py` 保证前端发出的状态对象 class 真实存在。

人工看点：切换 `data-ui-state` 时对象占位高度、行高、列宽不抖动。
