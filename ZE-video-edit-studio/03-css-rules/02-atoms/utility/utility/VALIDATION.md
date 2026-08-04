# utility 校验

- `python3 scripts/css_module_structure_check.py`：检查对象文件夹、说明、规则、校验和扩展文档是否齐全。
- `python3 scripts/css_layer_guard.py`：检查层级、令牌使用、原子重定义、状态样式的视觉来源。
- `python3 scripts/css_scroll_affordance_check.py`：检查软滚动内容流是否使用 `ze-scroll-fade-*` 或 L2 shell mask fade，并检查滚动条 token/utility 是否统一为 thin。
- `python3 scripts/css_typography_contract_check.py`：检查字号、字重、字距是否走 token/合同。
- `python3 scripts/css_contract_check.py`：检查 TSX 发出的 `.ze-*` / `--ze-*` 是否真实存在。
- 改图标对象时还要跑 `python3 scripts/icon_tokens_check.py`。
