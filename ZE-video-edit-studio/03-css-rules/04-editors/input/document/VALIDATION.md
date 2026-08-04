# document 校验

- `python3 scripts/css_module_structure_check.py`：检查对象文件夹、说明、规则、校验和扩展文档是否齐全。
- `python3 scripts/css_layer_guard.py`：检查层级、令牌使用、原子重定义、状态样式的视觉来源（本层须零令牌）。
- `python3 scripts/css_contract_check.py`：检查 TSX 发出的 `.ze-*` / `--ze-*` 是否真实存在。
- `python3 scripts/css_container_contract_check.py`：`.ze-document__body` 的 `flex:1`+`overflow-y:auto` 须带 `min-height:0`（R1 滚动归属）。
- `python3 scripts/editor_manifest_check.py`：document 须在 EDITOR_MANIFEST.json 声明，功能区 selector 须在本 code.css 存在。
