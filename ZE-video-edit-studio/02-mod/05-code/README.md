# 02-mod/05-code — 业务方代码

`02-mod/` 下 `01-document.md` / `02-entities.md` / `03-commands.md` / `04-layout.md` 是**声明**，本目录是**实现**：业务方在这写 Python / TypeScript 代码，把声明落地。

## 入口约定

每个 mod 模块导出一个 `register(...)` 函数，由 `ze_command` 启动时调用一次：

```python
# 02-mod/05-code/document_register.py
from sdk import DocumentRegistry

def register(registry: DocumentRegistry) -> None:
    """跟 02-mod/01-document.md 声明对齐 — entity_type / schema / mode / demo / read_only_sources."""
    ...

# 02-mod/05-code/commands_register.py
from sdk import CommandRegistry

def register(registry: CommandRegistry) -> None:
    """跟 02-mod/03-commands.md 声明对齐 — 注册每条 command 的 handler."""
    ...
```

启动顺序：`document_register` → `commands_register` → `layout_register` → `panels_register` → demo bootstrap。

## 推荐文件组织

```
02-mod/05-code/
├── document_register.py       # Document schema / collaboration_mode / demo / read-only sources
├── commands_register.py       # Command 注册 + handler 实现
├── handlers/                  # 复杂 command 的 handler 拆分文件
│   └── <command_name>.py
├── panels/                    # 自定义 panel (如 mod 不复用 framework 通用 panel)
│   └── <panel_name>.tsx
└── verify/                    # 业务自定义校验（超出 field-types 的复杂规则）
    └── <rule_name>.py
```

## 别在这干的事

- ❌ 直接读写持久化层（必须走 `Document` API — 见 [`../../01-core/01-data-structure/01-document/SPEC.md`](../../01-core/01-data-structure/01-document/SPEC.md) §8）
- ❌ 重新发明 `01-core/` 已有的抽象（Operation / Pipeline / Command 注册机制等）
- ❌ 修改 `01-core/` 的代码做 hack — 真有缺失能力，提去 core 改

## 自检

- [ ] 每个 register 函数跟对应 `.md` 声明逐项对齐（schema 没漂移）
- [ ] handler 全部走 Command → Operation 链路（不绕过 Document API）
- [ ] 业务自定义校验只覆盖 field-types 表达不了的部分
