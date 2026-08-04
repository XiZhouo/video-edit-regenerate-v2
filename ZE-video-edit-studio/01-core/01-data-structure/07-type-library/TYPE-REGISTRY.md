# TYPE-REGISTRY — 类型库词表单一真源

> 本表与 `type_library.list_fragments()` 双向一一对应,由 `scripts/type_registry_check.py`
> (GATE-TYPE-07)钉死:**登记了必须真有、真有必须登记、版本/标记不得失真**——文档撒谎结构性不可能。
> 改 fragments.py 必须同步改本表(反之亦然);版本法见 [SPEC.md](./SPEC.md) §4。

体例(机器区,镜像 DESIGN-SYSTEM §0.3):每行 `| 全 id | @版本 | 中文名 | 标记 |`。
标记词表:`closed`(闭枚举,加值=core MAJOR)/ `pinned=<名>`(落点字段名钉死)/
`sealed=<n>`(封印字段数)/ `collab=<bool>`(默认 collaboration_unit)/ `singleton`(单例锚)。

计数口径:T1 字段基元 **21 条**(media/mask/thumb/mesh 四件套计 1 条、占 4 个 id → 24 行)
+ 字段簇 3 + 原型 14 + 文档组合 8 = 49 个 id。

<!-- TYPE-REGISTRY:BEGIN -->

## T1 字段基元(field)

| id | 版本 | 名 | 标记 |
|---|---|---|---|
| `ze.field.bbox_xyxy` | @1 | 检测框 | — |
| `ze.field.class_id` | @1 | 类目号 | — |
| `ze.field.color_hex` | @1 | 颜色 | — |
| `ze.field.confidence` | @1 | 置信度 | — |
| `ze.field.easing` | @1 | 缓动 | — |
| `ze.field.keyframes` | @1 | 关键帧 | — |
| `ze.field.label` | @1 | 语义名 | — |
| `ze.field.mask` | @1 | 遮罩 | — |
| `ze.field.media` | @1 | 主媒体 | — |
| `ze.field.mesh` | @1 | 网格 | — |
| `ze.field.order` | @1 | 序号 | — |
| `ze.field.parent` | @1 | 父节点 | — |
| `ze.field.point2d` | @1 | 二维点 | — |
| `ze.field.prompt_block` | @1 | 提示词对 | — |
| `ze.field.rect_xywh` | @1 | 放置矩形 | — |
| `ze.field.run_status` | @1 | 执行状态 | closed pinned=status |
| `ze.field.size2d` | @1 | 像素尺寸 | — |
| `ze.field.stage_list` | @1 | 阶段表 | — |
| `ze.field.tags` | @1 | 标签集 | — |
| `ze.field.thumb` | @1 | 缩略图 | — |
| `ze.field.time_range` | @1 | 时间区间 | — |
| `ze.field.timestamp_iso` | @1 | 时间点 | — |
| `ze.field.transform2d` | @1 | 渲染变换 | — |
| `ze.field.workflow_status` | @1 | 人审状态 | closed pinned=status |

## T1 字段簇(cluster)

| id | 版本 | 名 | 标记 |
|---|---|---|---|
| `ze.cluster.audio` | @1 | 音频簇 | — |
| `ze.cluster.rect` | @1 | 几何簇 | — |
| `ze.cluster.run` | @1 | 执行簇 | — |

## T2 实体原型(archetype)

| id | 版本 | 名 | 标记 |
|---|---|---|---|
| `ze.archetype.annotation` | @1 | 标注 | sealed=9 collab=false |
| `ze.archetype.asset_item` | @1 | 媒资 | sealed=5 collab=true |
| `ze.archetype.block` | @1 | 块 | sealed=11 collab=true |
| `ze.archetype.chapter` | @1 | 章节 | sealed=3 collab=true |
| `ze.archetype.clip` | @1 | 片段 | sealed=5 collab=true |
| `ze.archetype.data_query` | @1 | 数据查询 | sealed=4 collab=true |
| `ze.archetype.graph_node` | @1 | 图节点 | sealed=3 collab=true |
| `ze.archetype.layer` | @1 | 图层 | sealed=16 collab=true |
| `ze.archetype.placed_object` | @1 | 放置物 | sealed=7 collab=true |
| `ze.archetype.prompt_preset` | @1 | 提示词预设 | sealed=3 collab=true |
| `ze.archetype.review` | @1 | 审核 | sealed=3 collab=true |
| `ze.archetype.scene` | @1 | 场景锚 | sealed=4 collab=true singleton |
| `ze.archetype.task` | @1 | 任务 | sealed=3 collab=false |
| `ze.archetype.track` | @1 | 轨道 | sealed=2 collab=true |

## T3 文档组合(doc)

| id | 版本 | 名 | 标记 |
|---|---|---|---|
| `ze.doc.dataset` | @1 | 查询工作簿 | — |
| `ze.doc.document` | @1 | 块文档 | — |
| `ze.doc.knowledge` | @1 | 知识空间 | — |
| `ze.doc.layered_image` | @1 | 分层画布 | singleton |
| `ze.doc.node_graph` | @1 | 节点图 | — |
| `ze.doc.review_queue` | @1 | 审核队列 | — |
| `ze.doc.scene` | @1 | 场景关卡 | singleton |
| `ze.doc.timeline` | @1 | 时间线成片 | singleton |

<!-- TYPE-REGISTRY:END -->

## 附则一:asset_ref 角色槽

四件套(media/mask/thumb/mesh)是注册基元;**次常用角色同法直接落 archetype**、不另立基元
(命名法 4:按角色命名、无类型后缀):

| 角色 | 落点 | 语义 |
|---|---|---|
| `crop` | annotation | 标注裁切图(标注管线必产物,Gallery thumbField 读它) |
| `before` / `after` | review | 处理前后对照图 |
| `background` | scene | 画布背景底图(canvas2d bgField 读它) |
| `rows` | data_query | 查询结果行 blob(结果进 asset 不进 entity) |
| `refs[]` | prompt_preset | 参考图数组(array of asset_ref) |

## 附则二:裸 id 关系白名单

库内引用一律 `entity_ref`(`{"$ref":"type/id"}`),**唯一例外**:

| 字段 | 形状 | 为什么豁免 |
|---|---|---|
| `graph_node.deps` | `string[]`(上游节点 id) | GraphCanvasZone.depsOf 直接当 string[] 画边——编辑器邻接语义本质;边实体 v1 不立 |

新增裸 id 关系须在此登记并给出裁定理由,否则按违例处理。

## 附则三:编辑器拥有的开放词表

下列字段值域归编辑器演化、库存 `string` 不闭枚举(编辑器加值不应触发库版本):

| 字段 | 当前值域所在 |
|---|---|
| `layer.kind` / `scene.layers[].kind` | canvas2dModel.ts `Canvas2dLayerKind` |
| `block.blockType` | 03-editor `editors/document/blockRegistry`(块组件注册表;P0.3 落地) |
| `layer.blendMode` | canvas2dModel.ts `Canvas2dBlendMode` |
| `stage_list[].status` | taskStages 读法(done/error/active/idle) |

## 附则四:图层双形态(SPEC §3 之二)

`layer@1` 的 **entity form 是画布主形态**(2026-06-11 裁定;`ze.doc.layered_image` 带 `layer` 桶,
图层列表=卡片列表编辑器实例,选层经全局 selection 驱动 form/画布);`scene@1.layers` 内联栈
弃用于画布编辑,仅信封 legacy 折展兼容(导出展开归一 order,导入按目标是否声明 layer 桶分流)。
