# timeline 扩展规则

- 新增能力先问是不是本对象职责；如果是，优先新增 `data-*` 闭集分支，而不是新建近义 class。
- 新增状态时先补 L1 `--ze-state-*` token，再在本对象消费；不要在对象里写一次性颜色。
- 新增子元素用 BEM element（例如 `__label`），新增变体用 data 属性。
- `data-draft="true"` 是瞬时创建预览的闭集语义；若以后增加 resize/move 草稿，优先另加正交事实属性，不要把 draft 改成持久状态或在 mod 复制一套草稿皮肤。
- draft 视觉若需调整，只改本 L2 原子并消费 L1 token；editor 仍只传位置/尺寸等运行时结构值。
- 任何会被 mod 消费的 class / data 语义变化，都要同步 `_contract.json` 与迁移记录。
