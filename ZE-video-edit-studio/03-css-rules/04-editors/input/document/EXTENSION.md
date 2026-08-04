# document 扩展规则

- 新增能力先问是不是本对象职责；块文档=渲块流 + 逐块编辑 + 增删（M1 补拖拽重排/缩进/4 块种/slash 菜单）。
- **新块种类**先在 L2 加原子（如 callout/code-block/checklist/figure 各自的 `.ze-*` 原子），再在 `03-editor/editors/document/blockRegistry` 注册渲染——块视觉只住 L2，本 L4 不为某块种写视觉。
- 新增子元素用 BEM element（例如 `__handle`），新增变体用 `data-*` 属性闭集；不新建近义 class。
- mod 自定义块经 `view_state.blockRenderers → ctx.renderers`（开放集，同 scene3d renderer 注入），不改 core。
- 任何会被 mod 消费的 class / data 语义变化，都要同步 `_contract.json` 与迁移记录。
