# SPEC — 统一数据类型库(07-type-library)

> 数据层复刻 CSS 设计体系走过的路:散落形状 → 分层标准库(基元→原型→组合)→ 引用组合 +
> 门禁守恒 → **同词表即互通**。与统一节点模型同构:每个类型节点 = 对下层片段的组合引用 +
> 自身参数面(schema 约束)。
>
> 内容本体:[fragments.py](./fragments.py)(纯字面量) · 机制:[type_library.py](./type_library.py) ·
> 词表真源:[TYPE-REGISTRY.md](./TYPE-REGISTRY.md) · 扩展总纲:EXTENSION-RULES P-2(加性 only)/
> P-4(扩展点闭集)。

## §1 分层

| 层 | 是什么 | 数量 | id 语法 |
|---|---|---|---|
| T1 字段基元 | 反复出现的字段形状(含 3 个共享字段簇) | 21 条+3 簇 | `ze.field.<snake>@<N>` / `ze.cluster.<snake>@<N>` |
| T2 实体原型 | 组合 T1 而成的完整 entity schema,带编辑器绑定 | 13 | `ze.archetype.<snake>@<N>` |
| T3 文档组合 | 成员原型 + collaboration_mode + 单例锚声明 | 7 | `ze.doc.<snake>@<N>` |
| 信封 | 跨 skill 交换格式(.ze-pack) | 七域 | `ze.manifest.<domain>.v<N>`(实现见 envelope.py) |

**词表红线**:只用 04-field-types 已实装类型(KNOWN_TYPES 九种)与 5 个注册 format——未知
format 在 `doc.add` 即抛 `schema_violation`(field_types._FORMAT_PATTERNS)。

**平铺对约定**:实体顶层几何/时间等用**平铺字段**(CAS 按键合并粒度细 + FormZone 自动控件);
数组内嵌套才用对象形(rect_xywh / prompt_block)。

## §2 命名六法

1. **snake_case**:库铸多词字段一律 snake(`class_id` / `negative_prompt` / `source_in` /
   `fade_in`)。证据:Entity 信封本身 snake;vision 草案对齐 supervision/COCO;后端 Python。
2. **编辑器契约优先例外**:被 core 编辑器按固定拼写直读(无 mapping prop)的字段沿用原拼写。
   全量核对唯一命中:`layer` 1:1 镜像 Canvas2dLayer(camelCase:blendMode/assetRef/maskRef/
   flipX/flipY,见 canvas2dModel.ts)。
3. **schema 约束键不动**:minLength/maxItems 等 JSON Schema 驼峰是 04-field-types 已定词表。
4. **asset_ref 字段按角色命名、无类型后缀**:`mask` 不是 `mask_asset_ref`(值形状自述类型;
   编辑器映射也按角色读)。次常用角色登记 TYPE-REGISTRY 附则一。
5. **id 语法**:见 §1 表;entity_type 桶名默认=archetype 短名(mod 可改,见 §6 纪律③)。
6. **mod 自由字段 `x_` 前缀**;archetype 顶层**不设** `additionalProperties:false`(否则
   x_ 进不来),前缀纪律由 zt.archetype 在 declare 时静态把守;值对象 fragment(rect_xywh 等)
   则封死形状。

## §3 文档粒度四法 + 整合裁定

- **法 A(粒度)**:document = 一个**独立流转的工作单元**——能被单独打开、协作、审核、导出
  交换的最小完整作品。一关卡、一条成片、一张分层图、一个知识空间各是一个 document;
  「项目=一批关卡」这类聚合**不进 document**。
- **法 B(单例锚)**:作品型 document 用**一个单例锚实体**(anchor)承载作品级属性(尺寸/
  背景/状态/难度/fps…)——锚是实体而非 doc.config,因此可编辑、可 undo、过 schema、进信封。
  preset 标 `singleton`,信封导入校验「恰一个」。
- **法 C(容器型)**:批次/队列型 document(审核台、查询工作簿)明确声明为容器型——无单例锚,
  批量实体平铺;它们的「作品」就是这份清单本身。
- **法 D(跨作品组织)**:批次浏览/项目归组走 **project picker 多文档 + tags 命名约定**;
  跨文档汇览用 read_only_sources(指向各 doc 的导出)或容器型工作台 doc,**永不**把多个作品
  塞回一个 document。

**范式改造案例(seek-find-object-gen)**:旧「一项目 N 关卡」→ N 个 `ze.doc.scene` 文档
(每关一档:scene 锚 + placed_object 群 + asset_item);批次审核由一个 `ze.doc.review_queue`
容器档承接。

**整合裁定**(设计优先法:旧 skill 现状不是真理,田野是证据不是规范):
1. level + layered_image 的 canvas 复合 → 统一 `scene@1` 单例锚(一个锚原型服务 scene-doc 与
   layered_image-doc 两形态)。
2. placed_object 甩掉 level 引用(单关单档后锚即上下文)。
3. run 簇共享(task 与 data_query 共组)。
4. 音频簇从 clip 抽出(未来 audio 工具复用)。
5. 几何簇共享(placed_object 等;layer 因法 2 例外单列,张力已注记)。
6. **重检而保留**(裁定非沿袭):clip.track 整数泳道(CRDT 友好、免级联 join);
   graph_node.deps 裸 id(编辑器邻接语义本质,白名单见 TYPE-REGISTRY 附则二)。

## §3 之二 图层双形态裁定(2026-06-11,推翻初版)

- **entity form 是画布主形态**:`ze.doc.layered_image` 带 `layer` 桶;图层列表=卡片列表编辑器
  (gallery compact-list)实例,选层走全局 `selection[<layer 桶>]` → form 属性面板/画布活动层
  联动——满足「卡片列表=实体选择器与操作器、选中驱动属性面板」三原则;逐层 CAS、逐层协作单元。
- **inline form(scene.layers 内联栈)弃用于画布编辑**:字段保留(非 sealed;删=MAJOR 不做),
  仅作信封 legacy 折展兼容——导出仍 1:1 展开为 layer 实体(order=栈位,**连续整数归一**),
  导入对无 layer 桶的 legacy 目标折回内联栈、对桶感知目标直收实体。
- 初版「内联栈为主(编辑器原生零适配)」的理由被三原则推翻:零适配换来的是图层选择困在
  画布组件局部 state、属性面板进不去、卡片列表接不上——架构成本高于适配成本。

## §4 版本法

- 库随 `core.lock.json`(sdk/_contract.py INTERFACE_VERSION)。**已发布 fragment/archetype 的
  删、改名、语义变、约束收紧、必填化 = core MAJOR**(Skill.Sync 报 incompat);**加性 = MINOR**
  (compat_bump 自动下发);措辞 = PATCH。
- fragment 自带 `@N` 大版本(无 minor;兼容改动由 core MINOR 承运);同树可并存 `clip@1`/`clip@2`,
  旧版标 deprecated(先 deprecate 后删,删那刻才 MAJOR——css 15.6.0 deprecated 先例同节奏)。
- **闭枚举加值 = MAJOR**(与 css「加性=MINOR」家规的显式分歧点):旧库 validator 对枚举外值在
  `doc.add` 即抛——新数据进旧 skill 失败。registry 标 `closed` 的枚举(workflow_status /
  run_status)受此法;非 closed 枚举(easing/kind/dialect)加值=MINOR,但要自问下游旧 validator
  会怎么对待新值。
- MIGRATIONS 条目加固定 grep 行体例:
  `类型库: +ze.field.<x>@1, ~ze.archetype.<y>@1(+字段), deprecate ze.field.<z>@1`。

### worked examples

1. **加基元**(MINOR):+`ze.field.depth_map`(新 asset_ref 角色槽)→ fragments.py 加条目 +
   TYPE-REGISTRY 加行 + core 1.x+1.0;旧 mod 零影响。
2. **闭枚举加值**(MAJOR):run_status 加 `paused` → 旧 skill 的冻结 schema 会拒含 paused 的
   导入数据 → core (x+1).0.0 + MIGRATIONS 写迁移指引。
3. **改名**(双写一个 MINOR 周期后删=MAJOR):`confidence` 改名 → 先加新名字段 + 旧名标
   deprecated(MINOR);下一个 MAJOR 周期删旧名。

## §5 组合机制(核心裁决)

**解析发生在 `declare_entity_type` 之前**(zt 调用内)——DocumentRegistry / field_types
validator / 编辑器**全部零改动**(x-* 注解键 validator 不读,已逐键验证;additionalProperties
约束数据非 schema 元数据)。

`document.json` 落盘的 data_schema 是**平铺 resolved dict + x-ze-* 注解**:
(a) Document.open 直接建 validator,旧项目永不依赖库当前版本(schema 随文档冻结);
(b) 注解让门禁/export 机器识别引用(注解词表见 type_library.py 模块 docstring)。

```python
from sdk import types as zt

def register(registry):
    zt.apply_preset(registry, "scene")                       # 整 preset
    registry.declare_entity_type(                            # 或逐原型组合
        name="shot",
        schema=zt.archetype("asset_item", extend={"x_caption": {"type": "string"}}),
        collaboration_unit=True,
    )
```

JSON 镜像(docs/AI/信封次形态,与 Python 面同一条校验代码路径):
`{"$use": "ze.archetype.clip@1", "extend": {"x_speaker": {"$use": "ze.field.label@1"}}}`
→ `zt.resolve(node)`。裸短名歧义(scene 同为 archetype/doc)必须用全 id。

失败模式(mod register 即炸,可读):`type_fragment_unknown`(带近似候选)/
`type_version_mismatch` / `type_extension_violation` / `type_seal_violation`。

## §6 扩展纪律四条

1. **加性 only**:mod 新字段必须 `x_` 前缀(或命中 archetype 声明的 extension_slots 闭集——
   v1 全部为空集);extend 碰撞既有字段=违例。
2. **不得重定义既有字段**:sealed 连 narrow 都不行(`type_seal_violation`);非 sealed 只紧
   不松(同 type;min 只升 max 只降;enum 子集;required 只能加——assert_tightens 单一代码路径)。
3. **entity_type 名 = mod 自由别名**:绑定靠 `x-ze-archetype` 注解不靠名字(subtitle 与 bgm
   可同指 clip@1);apply_preset(rename=) 自动重写库内 entity_ref 的 `to:` 指向。
4. **不用库完全合法**(逃生舱):手写 schema 的 mod 不受任何新约束,只是不享受信封互认与
   词表互通。

## §7 编辑器绑定承重墙

每个 archetype 的 `binding` 常量表是**逐 prop 核对过真实编辑器**的字段映射(信息面,不改
编辑器本身)。**字段名被 view_state 映射依赖,内容转写时不得漂移**:

- `status` 落点名钉死(pinned):ReviewEditor statusField / taskStages 直读 `data.status`。
- clip 的 track/start/duration ↔ Timeline fields;placed_object 的 x/y/w/h/rotation ↔
  canvas2d xField…rotationField;data_query 的 sql/dialect ↔ Code codeField/langField。
- 诚实声明:annotation.bbox_xyxy **不能 gizmo 直编**(互通优先)——改框走命令或转 placed_object。

## §8 互通三级

| 级 | 机制 | 状态源 |
|---|---|---|
| P0 | 信封 .ze-pack 导出导入(公共壳+七域;导入五步算法;整次导入一键 undo;命令 manifest.export/import 由 bootstrap 注入) | envelope.py + GATE-TYPE-08(test_envelope.py) |
| P1 | read_only_sources 直读对方导出(会话视图:每次装载经 make_pack_loader 重物化+刷新购销,目录指向取最新包,blob 灌共享池) | envelope.py materialize_read_only_sources + bootstrap.build_project_context |
| P2 | cross-skill-call(信封即 wire format) | 04-cross-skill-call SPEC §6 开放决策 |

**自描述包协商层(broker;与三级正交的语义深度)**:`manifest.preview` 出 ingest proposal(按
archetype 撮合,非桶名)、`manifest.import` 的 `mode="opportunistic"` 让**异类**工具收下「它能用的
子集」、其余带 warning 丢弃。**L0** 原始资产恒收;**L2** 整原型精确匹配(必填闸 + 必填 ref 闭包 + AP-03
锚子物丢弃);**L1** 单角色收割经 mod 声明的 `harvest_rules`(`when_role`→`into_bucket`+`supply_required`
补标量必填)启用 —— **诚实修正:into_bucket 含必填 entity_ref 的收割被拒**(常量补不出活引用,故设计举的
bbox→annotation 不成立;media→asset_item 补 kind 成立)。函数:`compute_ingest_proposal` /
`filter_manifest_to_proposal` / `derive_accept_profile` / `proposal_for_pack`(envelope.py)+
`DocumentRegistry.declare_harvest_rule` / `validate_harvest_rule`(document.py);完整设计见
[plans/cross-skill-interop-broker-design.md](../../../../plans/cross-skill-interop-broker-design.md)。

**AP-02 红线**:信封是**拷贝语义**,不产生跨文档活引用;包外悬挂引用**清除**(键移除——
validator 不容裸 null)+ warning,必填引用被清的实体降级丢弃 + warning。
**AP-03 红线**:singleton 锚的子物(placed_object/layer/clip/track 等)**默认不可 L2 收割进外来锚**
——子物对锚无回指,跨锚收割会静默错位坐标(`foreign_anchor_child` drop);除非目标同收该锚(受单例守恒
约束)。
**单例锚守恒**(粒度法 B):导入后每个 singleton 桶仍 ≤1,否则结构化拒绝
`manifest_singleton_conflict`(一作品=一 document,另开项目再导入)。

### §8.1 manifest 文件效果与提交顺序（W05）

`manifest.export` 遵守一条不可跨越的顺序：

1. **producer**：从 Document/AssetStore 读取并冻结不可变快照（manifest JSON bytes + 按 sha
   排序的 blob bytes tuple），生成白名单文件名；只返回一个零 patch Operation 与一个
   `DeferredAction`。此时 Operation.external 只能写 `status=pending`，target_ref 只含安全文件名、
   schema 与数量，不含绝对路径、实体内容、密钥或用户输入。
2. **business commit**：统一 `CommitStore` 先持久化 Document，再持久化 OpLog。任何结果不是
   fully durable committed，写包动作均为 skipped，最终文件不得出现。
3. **event acceptance**：canonical event 必须已发布，或 publisher 明确返回 durable pending。
   `FAILED` 不执行写包；不能因为导出文件尚未写而提前对外广播“已导出”。
4. **after commit**：动作从冻结快照重建输入，调用受 effect guard 保护的 `write_pack`，以
   `.partial` 原子换入最终路径。动作失败只产生 ActionReceipt.failed，已经成立的业务
   CommitReceipt 不回滚、不重放。

| 边界结果 | 业务状态 | event | 最终 `.ze-pack` | 调用方动作 |
|---|---|---|---|---|
| Document/OpLog 证明未落盘 | aborted | 不发布 | 无 | 可按 receipt 的 retry_safe 重新发起 |
| durability unknown/部分落盘 | indeterminate | 不发布 | 无 | 禁止自动重放，进入修复/对账 |
| committed，但 event failed | committed | failed | 无 | 修复 event，不重放业务命令 |
| committed，event accepted，写包失败 | committed | published/pending | 无 | 修复/重跑同一个 action |
| committed，event accepted，写包成功 | committed | published/pending | 有 | 完成 |

幂等约束：同一个 action 的路径与 snapshot 固定；zip entry 顺序、时间戳和权限元数据固定，
重复执行所得文件逐字节相同。`write_pack` 在 producer phase 被直接调用时稳定拒绝
`producer_effect_forbidden`，防止未来 handler 回归成“先写文件、再尝试提交”。

输入能力约束：`manifest.preview/import` 的 `args.path` 只允许 `source=system`；user/ai
必须使用上传后得到的 `args.asset`。越权路径不回显其内容或具体值，稳定错误码为
`manifest_path_forbidden`；畸形 asset_ref 为 `manifest_asset_invalid`。命令自己的 source ACL
仍可更早拒绝（例如当前 `manifest.import` 不允许 ai）。

**现阶段诚实边界**：`DeferredAction` 是 ephemeral 的进程内对象。业务已提交但动作尚未运行时
进程崩溃，当前版本不能在重启后自动找回；W06 必须用 durable action journal/outbox 记录 action
id、snapshot/blob 引用、目标相对路径、尝试次数与最终结果，再由 repair worker 幂等续跑。
在 W06 完成前，不能把导出承诺描述为 crash-recoverable。

导入的 `AssetStore.put` 仍发生在 import producer 内。内容寻址让重复 put 的数据结果幂等，
但它还不是完整事务参与者：后续文档提交失败可能留下无引用 blob。W06 应改为
content-addressed prepare（先校验/暂存、记录 sha）→ business commit → 引用/清理 finalize；
本次只明确债务，不把它混入导出边界改造。

## §9 门禁(注册:_protocol/AUDIT-GATES.md + audit_gates.py;07/09 在 scripts/type_registry_check.py)

| Gate | 检查什么 |
|---|---|
| GATE-TYPE-01 | id 格式正则 + 三写法等价 + 畸形拒绝 |
| GATE-TYPE-02 | fragment 自验:每条 example 灌真 make_validator(13 原型全量) |
| GATE-TYPE-03 | 深拷贝:改返回值不污染库 |
| GATE-TYPE-04 | extend 加性 only 负例(非 x_ / 约束键) |
| GATE-TYPE-05 | seal 与 tighten-only 正负例 |
| GATE-TYPE-06 | provenance 注解必在(x-ze-use / x-ze-archetype / x-ze-sealed) |
| GATE-TYPE-07 | TYPE-REGISTRY fenced 表 ⇄ list_fragments() 双向一一对应+版本/标记不失真 |
| GATE-TYPE-08 | 信封 round-trip 逐字段保真 + sha 不变 + 版本不符结构化拒绝(envelope) |
| GATE-TYPE-09 | mod archetype 注解防漂移:带注解 schema 的库属字段必须是库内 fragment@version 的合法收紧(sealed 须全等) |
| GATE-TYPE-10 | broker 协商层:opportunistic 导入恰好落地 proposal.accept、不半导入 required_field_unmet 实体、锚子物按 AP-03 丢弃、L0 资产可读(envelope;详尽套件 test_broker.py) |
| GATE-TYPE-11 | broker 收割规则(L1)合法性:build_document fail-fast 拦非法 harvest_rule(必填 entity_ref / 未覆盖必填 / 坏角色 / 桶未声明);下游真把关(document.validate_harvest_rule) |

## §10 已知残差(诚实声明)

- ~~taskStages 别名 / ReviewEditor draft+in_review 标签~~ 已兑现(taskStages.RUN_STATUS_ALIAS
  + editor.stDraft/stInReview,core 1.26.0)。
- 三维域 v1 无独立 preset:以 asset_item(kind=model3d)+Scene3d{meshField} 承载,待 splat/3d
  系列 v3 化后立 ze.doc.scene3d(加性)。
- TYPE-REGISTRY.md 是 .md 不进 core 指纹(skill_sync 只哈希 .py)——词表漂移由 GATE-TYPE-07
  拦在 verify_all 而非 sync(接受残差)。
- FormZone 对 bbox 数组渲染落 JSON textarea 兜底(defer:空间编辑有 canvas2d 承接;独立 bbox
  控件等 ≥2 消费者)。
- TS EntityTypeSchema 无索引签名(运行时无害;要补也是一行加性)。
- manifest 导出提交后动作尚无 durable journal/outbox，崩溃恢复归 W06；导入 blob put 尚是
  content-addressed prepare 债务，失败可留无引用对象（可 GC，不等于事务已完成）。
