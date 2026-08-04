# 07-type-library — 统一数据类型库

把全系列 skill 反复出现的数据形状收成分层标准库:**T1 字段基元(21+3 簇)→ T2 实体原型(13)
→ T3 文档组合(7)**。mod 通过引用组合声明自己的 document——同词表即跨 skill 互通(信封交换
见 §8)。规则本体见 [SPEC.md](./SPEC.md),词表单一真源见 [TYPE-REGISTRY.md](./TYPE-REGISTRY.md)。

| 文件 | 角色 |
|---|---|
| `fragments.py` | 内容本体(纯 dict 字面量,零逻辑) |
| `type_library.py` | resolver + zt 组合 API(解析/extend/narrow/preset/失败模式) |
| `test_type_library.py` | GATE-TYPE-01..06(07/09 在 scripts/type_registry_check.py) |
| `envelope.py` | 信封 .ze-pack 导出导入(P0 互通;七域;manifest.export/import 命令)+ broker 协商层(manifest.preview / opportunistic import;异类工具识别可收子集)；导出文件经提交后动作落盘 |
| `test_envelope.py` | GATE-TYPE-08(round-trip 保真 + 五步导入 normative 全谱 + 导出提交/事件/动作故障矩阵) |
| `test_broker.py` | GATE-TYPE-10/11(自描述包协商:preview proposal + opportunistic 导入;L0/窄L2/L1 harvest;§3.3 自毁防护 + harvest_rule 合法性) |

## 怎么用

mod 在 `02-mod/05-code/document_register.py` 里组合声明(经 sdk 门面):

```python
from sdk import types as zt

def register(registry):
    # 整 preset:一行得到全套 entity_type + collaboration_mode
    zt.apply_preset(registry, "scene")

    # 或逐原型组合:库属字段零手写,业务私有字段 x_ 前缀加性扩展
    registry.declare_entity_type(
        name="shot",                      # 桶名是 mod 自由别名(绑定靠注解)
        schema=zt.archetype("asset_item", extend={"x_caption": {"type": "string"}}),
        collaboration_unit=True,
    )

    # 单字段/平铺组也可直接取用
    my_schema = {"type": "object", "properties": {
        "label": zt.field("label"),
        **zt.cluster("rect", required=["x", "y", "w", "h"]),
    }}
```

- 改桶名同时保住引用:`zt.apply_preset(registry, "timeline", rename={"asset_item": "footage"})`
  (自动重写库内 entity_ref 的 `to:` 指向)。
- 收紧非 sealed 字段:`zt.archetype("clip", narrow={"source_in": {"minimum": 1}})`(只紧不松)。
- 先查 [TYPE-REGISTRY.md](./TYPE-REGISTRY.md) 再自造——已有形状必须引用,新形状 ≥2 消费方
  才进库(EXTENSION-RULES P-3)。
- 不用库完全合法(逃生舱),只是不享受信封互认;详细纪律与失败模式见 [SPEC.md](./SPEC.md) §5–§6。

## manifest 命令的文件边界

`manifest.export` 的命令处理器只读取当下文档，冻结一份不可变的 manifest/blob 值快照，
预分配一个白名单文件名，并返回 `status=pending` 的零 patch Operation。它不会在 producer
阶段创建文件。只有业务 Document 与 OpLog 已耐久、canonical event 已发布或被耐久接收后，
`CommitCoordinator` 才运行 `DeferredAction` 调 `write_pack`。存储未落盘或事件失败时没有最终
`.ze-pack`；文件动作失败时业务提交仍成立，动作结果单独报错，调用方不得重放业务命令。

同一动作重跑复用同一路径和同一不可变快照；zip 时间戳与条目顺序固定，因此内容逐字节一致。
`write_pack` 自身也受 producer effect guard 保护，绕开 `DeferredAction` 直写会得到
`producer_effect_forbidden`。当前 `DeferredAction` 仍是进程内临时队列：提交后、动作前若进程
崩溃，重启不会自动补写；持久 action journal / outbox 是 W06 的明确工作，不把现状说成已恢复。

`manifest.preview/import` 的 `args.path` 是宿主内部能力，仅 `source=system` 可用；user/ai
必须先上传包并传 `args.asset`，否则稳定返回 `manifest_path_forbidden`。这条边界阻止交互请求
把服务器任意文件当作导入包读取。
