# 04-editors — 单个编辑器

> L4 只管“某一种编辑器内部怎么组织”。工作台壳、Header、Footer、Layout、账号面已经下沉到 [05-compositions](../05-compositions/)。

## 目录

- `shell/editor/code.css`：编辑器共享基座。
- `shell/spatial/code.css`：空间类编辑器遗留指针，真实空间 UI 对象在 L2 `data/spatial`。
- `data/table/code.css`、`data/chart/code.css`：数据型编辑器结构。
- `input/form/code.css`、`input/text/code.css`、`input/code/code.css`：输入型编辑器结构。
- `media/asset/code.css`、`media/media/code.css`、`media/scene3d/code.css`、`media/compare/code.css`：媒体型编辑器结构。
- `workflow/task/code.css`、`workflow/timeline/code.css`、`workflow/graph/code.css`、`workflow/pipeline/code.css`、`workflow/review/code.css`：流程型编辑器结构。
- `gallery/gallery/code.css`：图库编辑器结构。

每个对象文件夹都有 `README.md`、`RULES.md`、`VALIDATION.md`、`EXTENSION.md`。

## 规则

- L4 禁直接消费 `var(--ze-*)`；视觉交给 L2 UI 对象，L4 只保留结构、容器查询和编辑器专属布局。
- 单个编辑器不嵌另一个编辑器；跨编辑器组合去 L5 LayoutTree。
- 状态来自 entity/run，挂到对象自身的 `data-ui-state` / `data-run-phase`，不要在编辑器里额外做一套空态/执行中页面。

## 校验

- `python3 scripts/css_module_structure_check.py`
- `python3 scripts/css_layer_guard.py`
- `python3 scripts/css_container_contract_check.py`
- `python3 scripts/editor_manifest_check.py`
