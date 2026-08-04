# _protocol — 跨叶子协议层 + 校验体系

> 不是业务叶子。`_` 前缀表示**框架自检层**——汇总跨叶子契约 + 跑 audit gates。各业务叶子（document / entity / asset / ...）的 SPEC §9 引用本目录的 gate 编号。

## 在框架里的位置

- 上一级：[01-core/](../) — 框架抽象 + 默认实现
- 兄弟：[01-data-structure/](../01-data-structure/) / [02-data-flow/](../02-data-flow/) / [03-commands-operations/](../03-commands-operations/) / [04-user-facing/](../04-user-facing/)
- **跟业务叶子区别**：业务叶子提供机制（lock / queue / resolver / ...）；本目录**串联机制 + 跑校验**

## 本目录文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（导览）|
| [CHANGE-FLOW.md](./CHANGE-FLOW.md) | 数据变更全链路 SPEC：业务调用 → 落盘 → broadcast → 前端 entity 级精确重渲染 |
| [AUDIT-GATES.md](./AUDIT-GATES.md) | 全部 audit gate 注册表（含已实施 + 待加 gate）|
| [change_set.py](./change_set.py) | EntityChange + `affected_entities()` + `to_broadcast_payload()` |
| [effect_context.py](./effect_context.py) | producer / idle / after_commit 的 ContextVar 与外部副作用防火墙 |
| [history_authority.py](./history_authority.py) | bootstrap 绑定的 canonical history 进程内权威：精确 runtime graph、fresh OpLog head/source、一次性 replay/append ticket、freeze/retire/fork 边界与 typed failure |
| [pipeline_authority_codec.py](./pipeline_authority_codec.py) | Pipeline strict v2 Start/Control owned codec；只解析/冻结事实，不签 grant |
| [pipeline_authority_reducer.py](./pipeline_authority_reducer.py) | 完整 fresh OpLog chain/head 的纯 Pipeline 权限归约器；不读 manifest/Document，不签 grant |
| [pipeline_authority_commit.py](./pipeline_authority_commit.py) | private active-root sealed factory、exact append permit 与 committed+durable fresh attestation；attestation 不导出为 worker grant |
| [pipeline_delegated_mutation.py](./pipeline_delegated_mutation.py) | private dormant delegated mutation request；每次准备/消费重验 fresh history、原业务 policy、精确 item/entity/stage/字段/lifecycle/cardinality 与实际 worker，但不执行写入或签 grant |
| [mutation_patch.py](./mutation_patch.py) | Document history replay 与 Operation log 共用的中立、不可变 Patch carrier；Operation 只负责兼容 re-export |
| [mutation_policy_coverage.py](./mutation_policy_coverage.py) | persistence / command / Document session 共用的 dependency-free coverage identity；仅防框架误接线，不是 Python sandbox |
| [audit_gates.py](./audit_gates.py) | 140 个 runtime / surface-aware static gate check 函数 + CLI |
| [test_protocol.py](./test_protocol.py) | pytest 协议与 gate runner 回归套件 |
| [test_history_authority.py](./test_history_authority.py) | canonical history 绑定、source/replay/append ticket、耐久 suffix、typed unknown outcome、retire/fork 回归 |

## 当前状态

**✅ 已实装**

| 范畴 | 实装 |
|---|---|
| 全链路 SPEC | [CHANGE-FLOW.md](./CHANGE-FLOW.md) §1-§8 完整 |
| entity 级聚合 | `affected_entities(op, doc) → list[EntityChange]` 含 5 种 change_kind 推导 |
| broadcast 消息契约 | `to_broadcast_payload(op, changes, doc_id=)` 输出标准 JSON |
| **broadcast 实施** | SSE 通道已落地（[broker.py](../02-data-flow/01-realtime-sync/broker.py) + [server.py](../../server.py) `/api/events`），连接即推快照 |
| **前端订阅 + 重渲染** | 已落地（[webapp useDocument](../../webapp/src/lib/useDocument.ts) 按 entity 精确合并 + [04-user-facing/](../04-user-facing/)）|
| **undo / redo 控制器** | 已落地（[undo.py](../02-data-flow/04-history-rollback/undo.py) + `/api/undo`·`/api/redo`·`/api/history` + footer 按钮/⌘Z）|
| **canonical history authority** | 已落地：bootstrap 原子绑定一个 project generation 的 exact Document / OpLog / UndoController / CommitCoordinator / CommitStore；controller 与 Document 的私有 epoch/PID 安装失败会回滚已发布边，且同一 Document 不能换代重绑。受保护 replay 只消费同一 coordinator transaction 的 fresh source 与一次性 ticket；内部 fresh verifier还会逐对象核对 registry/controller/Document/store/OpLog 与当前 head，漂移即冻结并拒绝 |
| **Pipeline authority Batch 4（dormant）** | Batch 3 strict v2 sealed commit/fresh attestation 之上新增一次性 delegated mutation request。它在准备和消费时都重验 exact store/Document/history graph、当前完整历史归约、Start 原业务命令与 policy、work-item→entity、stage、字段/lifecycle/cardinality、实体版本和实际 worker Actor/source；历史、worker、policy 或实体变化都会让旧请求失效。请求会读取 reducer 从完整历史归约出的 Control facts，只用于拒绝 stale/terminal/reconcile-required；它不可复制/序列化/fork 继承且 `execution_authority=False`，不 dispatch、不变更数据、不签 grant、不注册 worker、不写 action/manifest/run、不签发或执行/应用 Control lifecycle，也不把 legacy lifecycle 冒充 v2 Control，动态创建仍拒绝。Batch 5 才拥有 grant registry 与 Supervisor lifecycle，marker/resolver 不能补猜权限 |
| audit gates 注册表 | [AUDIT-GATES.md](./AUDIT-GATES.md)（注册 162 个 gate 命名；140 个由 audit runner 实施，另有 TYPE-07/09 由 verify_all 强制）|
| gates 实施 | **140 个**：AI×4 / AST×6 / AUTH×10 / CAP×3 / CC×1 / CMD×5 / COMMIT×15 / CONF×1 / DB×21 / DOC×8 / ENT×3 / FT×7 / OP×6 / PIPE×6 / PROJECT×13 / PROTO×4 / RB×6 / RT×7 / TYPE×9 / UNDO×5 |
| static gates（前端契约门禁）| 已落地为独立脚本：[css](../../scripts/css_contract_check.py) / [ops](../../scripts/ops_contract_check.py) / [i18n](../../scripts/i18n_contract_check.py) 契约检查 + [verify_all.py](../../scripts/verify_all.py) 汇总 |
| CLI runner | `python audit_gates.py --all / --gate / --leaf` |

**⏳ 待补**

| 项 | 依赖 |
|---|---|
| 注册表里剩余 20 个尚无可执行实现的 gate 逐个落地 | 各叶子按需补（runner 140 + TYPE-07/09 2 + 待补 20 = 注册 162）|
| 更多 runtime gate（actor_log append-only / asset refs consistent 等）| 后续扩展 |
| 把 css/ops/i18n 契约门禁接进 CI（现 verify_all 手动跑）| CI 集成 |

### Surface-aware gate 适用范围

跨 surface gate 以根目录 `ZE_SURFACE.json` 为唯一能力声明，不再根据残留目录猜测产品形态。公共后端/runtime 断言始终执行；只有声明 `api` 或 `web` 才读取并要求 HTTP server 接线，只有声明 `web` 才读取 Web/TypeScript 消费端。因而合法的 `headless-cli` 与无 Web 的 `api-service` 不会因为缺少 `webapp/` 假红；反过来，一旦声明了 `web`，缺 sanitizer、SSE consumer、ACL UI 或媒体 ticket 消费代码仍会稳定 FAIL，而不是 SKIP 或抛未捕获的文件异常。

当前按这套矩阵执行的是 `GATE-PROJECT-05/06/08/09/10/11/12/13`、`GATE-DB-11/12/13/14/15/16/17/18/19/20/21`、`GATE-DOC-10`、`GATE-COMMIT-08/09/10/11/12/13/14/15`、`GATE-RT-07`、`GATE-AUTH-09/10` 与 `GATE-AST-07`。`GATE-PROJECT-09/10/11` 依次证明严格目录迁移屏障与 fork fd 归属、耐久 writer admission/freeze marker 与完整 live/deleted 快照、同一 EX→EX barrier 内的 S0→v4/current→S1→v5 fresh reconcile→S2。`GATE-DOC-10 / GATE-COMMIT-08/09/10/11/12/13/14/15 / GATE-DB-11..15` 逐层锁住 Document candidate、原子事务计划、历史 writer/reader、Worker 投影与 durable audit、V11 baseline 逐列投影、增量 cutover head、16 MiB carrier preflight、V12 fixed-SQL install写入闭包、plan-free全局cutover状态重建、database-only phase-1 head收口与V13 asset-provider phase事务；`GATE-DB-21`再冻结current-V13四类direct-predecessor carrier及共享纯行mapper。

`GATE-DB-16/17/18/19` 分别冻结历史 V8/V9/V10/V11 identity 与升级边界；`GATE-DB-20` 冻结历史V12的4张install-closure表与V1–V11前缀；`GATE-DB-21`证明current V13只在clean/authentic V12上追加asset-provider/audit/activation/mount四张direct-predecessor binding表，并用纯all-field manifest、STRICT/append-only正向证据guard和head反向guard阻止跨阶段拼装。current私有数据面是17张transaction表+36张production/runtime表（V8 25 + V9 3 + V12 4 + V13 4）；普通启动仍必须legacy、无runtime control、全表为空。

`GATE-PROJECT-13` 把 V12 pure closure、shared stored-manifest parser、V5/V8 provenance reader、install-closure reader、neutral transaction verifier、fixed install writer、全局cutover durable auditor、phase-1 head finalizer、V13 binding mapper与asset-provider writer，同既有production authority/writer/rooted reader及Worker projection/claim/payload writer/auditor一起纳入全树private forward/reverse边界；`GATE-COMMIT-12/13/14/15` 分别冻结install写事务、只读状态重建、database-only phase-1收口与phase 1→2 asset-provider事务。asset writer必须在自己的`BEGIN IMMEDIATE`内raw重证并只做fixed binding INSERT→head CAS，外部观察保持same-thread active且production issuer/caller为零。当前仍没有audit/activation/mount三个writer、cutover coordinator、authenticated performance checkpoint、production authority router、dispatcher、renew/expire/retry或pipeline/action worker execution，因此“能私有推进到phase 2”仍不等于“已经能切换生产authority”。`GATE-PROJECT-08` 始终审计 Repository/topology，仅在 api/web 被声明时追加 server metadata 消费检查；`GATE-AST-07` 的票据签名与域分离 runtime 对所有形态有效，Repository/HTTP 检查只对 api/web 生效，前端媒体 URL 检查只对 web 生效。

## 怎么用

### 看全链路

```bash
open 01-core/_protocol/CHANGE-FLOW.md
```

§2 是完整时序图，§4 是前端 entity 级精确重渲染协议。

### 跑校验

```bash
cd 01-core/_protocol/

# 跑全部 gate
python audit_gates.py --all
# ✅ GATE-DOC-01: ...
# ✅ GATE-DOC-02: ...
# ...
# 总计: 140; PASS 140, FAIL 0, SKIP 0

# 只跑指定 gate
python audit_gates.py --gate GATE-OP-02

# 只跑某叶子
python audit_gates.py --leaf DOC
python audit_gates.py --leaf PROTO
```

### 业务代码用 change_set

```python
from change_set import affected_entities, to_broadcast_payload

# 后端 dispatch 完拿到 op + doc
op = cmd_registry.dispatch(...)
op_log.append(op)

# entity 级聚合
changes = affected_entities(op, doc)

# 构造 broadcast payload (含 type='entity_change' + EntityChange 数组)
payload = to_broadcast_payload(op, changes, doc_id=doc.id)

# 推到 channel(SSE 已落地;运行时由 bootstrap 把 broker 注入 Context)
broker.publish_change(payload)
```

### 前端订阅（已落地于 webapp/src/lib/useDocument.ts）

```typescript
// EventSource 订 /api/events?project_id=...&token=...&since_seq=...;按 type 分流。
// entity_change 携带 broker seq + op_id, useDocument 用它确认事件流是否追上本地写入。
openEventStream({
  projectId,
  sinceSeq: lastSeq + 1,
  onPayload: (p) => {
    if (p.type !== 'entity_change') return
    for (const ch of p.changes) {
      // ch.change_kind: 'updated' / 'created' / 'soft_deleted' / ...
      // ch.snapshot: entity after 状态 → 合并进对应 (type, id) 列表重渲染
    }
  },
})
```

## 设计要点

- **不污染业务叶子**：业务叶子保持自治，跨叶子规则归本目录
- **协议优先于实施**：CHANGE-FLOW.md 定契约（签名 + 数据结构 + 时序）；实施散在各叶子
- **每条规则一个 gate**：可机器校验，跑 audit_gates.py 就能验
- **前端 entity 级渲染**：渲染单元 key = `(entity_type, entity_id)`，broadcast 消息按 entity 聚合
- **canonical history 只有一个运行时图**：bootstrap 用一次性 capability 绑定 exact Document / OpLog / UndoController / CommitCoordinator / CommitStore、project generation 与 core runtime revision。它不把另一实例、同路径 alias 或 fork 继承对象提升为权威
- **fresh source 与 durable write 分开证明**：一次 coordinator execute 固定一个 fresh OpLog head，UndoController 只从该 head 的同一不可变快照选 source；source ticket 精确绑定 source body → replay Operation，append permit 再逐 Operation 绑定 commit intent，最后以 durable suffix 的数量和 canonical body 逐条确认
- **失败语义靠类型，不靠字符串**：`HistoryAuthorityError` 固定 `code=history_authority_mismatch`，并显式携带 `reason`、`outcome_unknown`、`durability_phase` 与可选的 copied `commit_facts`。HTTP、MCP、CLI、Pipeline 与幂等账本消费 exact type/boolean，不能由 message 或 reason 猜能否重试
- **runtime authority 不冒充存储事务**：authority、trusted head、ticket 与 exact object identity 都只活在进程内，不进入 Document / Entity / Operation / business snapshot。当前 `FileCommitStore` 仍是 Document-generation-first、OpLog-second 的两个独立耐久域；authority 能发现并冻结不确定结果，但不能让两个域共享一个物理 commit point
- **`_` 前缀**：跟业务叶子区分，将来 `_audit/` / `_self_check/` 等扩展放同级

## 跟其他叶子的关系

- 各叶子 SPEC §9 可以**引用** AUDIT-GATES.md 的 gate 编号（不重复实施细节）
- 业务方写新 command / 新 skill 时**按 [CHANGE-FLOW.md](./CHANGE-FLOW.md) §2 走全链路**
- framework 维护者**周期跑 audit_gates.py** 确保跨叶子契约不漂

## 跑测试

```bash
cd 01-core/_protocol/
python -m pytest test_protocol.py -v          # 协议与 runner 回归
python audit_gates.py --all                    # 140 gates
```
