# badge 扩展规则

- 新增能力先问是不是本对象职责；如果是，优先新增 `data-*` 闭集分支，而不是新建近义 class。
- 新增状态时先补 L1 `--ze-state-*` token，再在本对象消费；不要在对象里写一次性颜色。
- 新增子元素用 BEM element（例如 `__label`），新增变体用 data 属性。
- 任何会被 mod 消费的 class / data 语义变化，都要同步 `_contract.json` 与迁移记录。
