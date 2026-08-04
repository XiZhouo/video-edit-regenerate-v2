# AI Coworker — AI 作为 actor 的安全约束

## 1. 这是什么

AI 在 ZE 框架里是 4 种 `source` 之一(`ai`),**既不是特权也不是黑盒**:它走和 `user` 完全相同的 `command → operation → OpLog` 通道,undo 不对它特殊对待。差别只在两点 —— 受 command 的 `sources_allowed` ACL 约束,且额外背 4 类安全约束:**溯源 / 安全过滤 / 人审 / 成本**。

本叶子是这 4 类约束的**注册表 + 契约**。多数底座已散落实装在 [02-operation](../02-operation/)(source 校验)、[02-entity](../../01-data-structure/02-entity/)(`AiProvenance`)、[03-pipeline](../03-pipeline/)(AI 提交走 CAS + 取消 + run-status)里;本 SPEC 把它们**收口成一份"AI actor 安全协议"**,并声明尚未实装的 3 道闸(安全过滤 / 人审 / 成本上限)。

> 业务白话:AI 帮业务同事把知识库某条标题改了 —— 框架要能回答"哪个模型改的、什么 prompt、花了多少 token/钱、谁批准的、有没有被安全过滤拦"。这条信息不是事后补的日志,而是写入数据时就字段级绑定的溯源。

### 为什么是独立叶子

| 问题 | 原因 |
|---|---|
| **AI 是 actor,不是新通道** | 不该给 AI 开后门;它必须复用 command/operation/CAS/undo 同一套,否则审计、撤销、并发安全全裂 |
| **溯源是字段级、不是操作级** | 同一次 AI 任务可能只改 entity 的某几个字段;provenance 要挂在字段上(`entity._provenance[field]`),不能只挂 op |
| **安全 4 类是横切关注点** | 溯源/过滤/人审/成本贯穿 command + entity + pipeline 三层,散在各处会漏;本叶子统一登记规则 |
| **多数是固化、少数是新规则** | provenance 数据结构、source ACL、CAS 提交、取消都已实装;本叶子主要"把现状定成规则"。过滤/人审/成本 3 道闸的**机制已由 `AiGovernor` 实装**(§4/5/6),剩 policy/规则库/output 随项目注入 |

## 2. AI 作为 source

```python
# 02-operation/operation.py
SOURCES = ("user", "ai", "sync", "system")
```

- source='ai' 的 Operation 与 user op **同构**(同一 `Operation` dataclass、同一 OpLog、同一 `reverse_patches`),因此**可被 undo**(见 [04-history-rollback](../../02-data-flow/04-history-rollback/));undo 系统不按 source 特殊分支。
- 差异仅在:① 受 command `sources_allowed` ACL 限(`GATE-CMD-05`:source 不在白名单 → `acl_denied`);② 额外背 §3–§6 四类约束。
- 占位/系统级写入用 `source='system'`(如 pipeline 起任务时建占位 entity),不混进 `ai`。

## 3. AI Provenance — 字段级溯源(✅ 已实装,本节固化为规则)

```python
# 01-data-structure/02-entity/entity.py
@dataclass(frozen=True)
class AiProvenance:
    actor: str                       # 谁触发(user_id)
    model: str
    model_version: str = ""
    prompt_template_id: str = ""
    prompt_args: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    token_in: int = 0
    token_out: int = 0
    ts: float = 0.0
    trace_id: str = ""
```

规则:
- **挂载点是字段级**:存于 `entity._provenance: dict[field → AiProvenance]`,经 `entity.update(patches, ai_provenance={field: AiProvenance(...)}, ...)` 写入,`entity.provenance(field)` 读出。
- **随 entity 序列化往返**:`to_dict()` 输出 `"provenance"` 段,`from_dict()` 还原(含前向兼容容错)。
- **source='ai' 的写入应当带 provenance**(至少 `actor` + `model`)。当前是"可带"非"必带" —— **完整性强制**(`GATE-AI-01`)列入 §11 ⏳,因为"哪些字段必填"是策略问题(见 §10)。

## 4. AI Safety — input 安全过滤(✅ 机制实装 AiGovernor;moderator 规则库=mod 挂点)

`AiGovernor.guard()`([ai_governor.py](ai_governor.py) `GATE-AI-04`)在 source='ai' 命令 dispatch 前,把 args 文本(`_text_of`)过一道 moderator 钩;moderator 返回 `False` 即 raise `ai_safety_blocked`,拦下整个 operation。

- **机制在 core**(`ai_governor.py` `_moderator` 钩 + `guard()`);governor 由 [bootstrap](../../../sdk/bootstrap.py) 挂到 `CommandRegistry(ai_governor=)`,治理未启用时=`None`、dispatch 行为不变。
- **moderator 规则库=mod 挂点**:`moderator(text)->bool` 由项目经可选 mod `ai_governor_register.py` 注入([bootstrap.py](../../../sdk/bootstrap.py) `_load_mod_optional`);**缺省 `None`=不做安全过滤**(core 只给挂点,不预置规则库、不默选)。
- **仍待补**:目前只审 **input**(args 文本);**output**(结果落 entity 前过滤)未做;命中处置(拒绝整 operation vs 脱敏放行)=**开放策略问题**(§10)。

## 5. Human Approval — 高风险动作人审(✅ 机制实装 AiGovernor;阈值/high-risk 标记=policy)

`AiGovernor.guard()`(`GATE-AI-05`):source='ai' 命令若 **估算 cost ≥ approval 阈值** 或 **`spec.ai_high_risk`**,返回 `GuardDecision(require_confirm=True)`→命令不直接 dispatch,进两段式确认:置 `pending_confirmation`、等网页发确认 nonce,经 [post_command_confirm](../../../server.py)(`server.py:2610`)→ [execute_confirmation](../../../sdk/persistence.py) 才真落。**AI 侧(MCP)拿不到 nonce,只能 pending 等人批**——推断≠执行的核心闸。

- **机制在 core**(`guard()` + pending/confirm 通道);**policy=挂点**:approval 阈值(`approval_threshold_usd`,None=关闭)/ "high-risk"标记(`spec.ai_high_risk`)由 04-ops config / command spec 声明。
- "什么算 high-risk"(硬删 / 级联删 / 批量改 / 跨 skill 调用)的具体划线仍是**开放策略问题**(§10)。

## 6. Cost Control — 单次成本上限(✅ 单次实装;日累计 ⏳ 待补)

`AiGovernor.guard()`(`GATE-AI-06`):每条 source='ai' 命令估算单次 cost(`estimate_cost`:`args['cost_usd']` 优先,否则 `spec.ai_cost_usd`),超 `per_call_cap_usd` 即 raise `ai_cost_exceeded`,拒该调用。

- **单次上限=已实装**(core 无状态);`per_call_cap_usd`=policy(None=关闭),04-ops config 注入。
- **日累计 / 熔断 ⏳ 待补**:需持久 ledger(本层无状态,`guard()` 只做单次)。建议与 [06-resource-budget](../../02-data-flow/06-resource-budget/) 的 `CapacityGate` / `ResourceContract` 对齐——**成本即加权容量**,复用 `acquire(cost)`,不另造限额引擎。默认额度 / 分 model 分档 / 日累计归属(per-user / skill / doc)/ 熔断恢复=**开放策略问题**(§10)。

## 7. 长任务 / 取消 / 版本安全(✅ 已实装,本节固化为规则)

AI 多为长任务,走 [03-pipeline](../03-pipeline/) 的 supervisor:

```
supervisor.start(work_list, stage, trace_id)   # 非阻塞
  ↓ 每项建占位 entity (source="system")，记 EntityRunStatus(phase="pending")
  ↓ worker 线程跑 stage (可 cancel: cancel_event + CancelledError)
  ↓ 成功 → _commit_ok(): dispatch("entity.update",
        source="ai", patches=..., ai_provenance=..., expected_version=<CAS>)
        ↓ 版本冲突 → error marker，绝不覆盖并发写
```

规则:
- **AI 提交必走 CAS**(`expected_version`):并发下不覆盖别人的在制编辑(`GATE-AI-02` / 复用 `GATE-ENT-01`)。
- **长任务相位**:`EntityRunStatus.phase ∈ ("pending","running","done","error")`([run_status.py](../../_protocol/run_status.py) `RUN_PHASES`);进度按文件系统产物派生(非内存百分比),崩溃/刷新可重建。
- **可取消**:`cancel_event` + 各 stage runner 传播 `CancelledError`;取消后占位回退,不留半成品。

## 8. Python API(现状)

已可调用(✅):
```python
AiProvenance(actor=..., model=..., cost_usd=..., token_in=..., token_out=..., trace_id=...)
entity.update(patches, ai_provenance={field: AiProvenance(...)}, expected_version=v, actor=a)
entity.provenance(field) -> AiProvenance | None
# AI 提交由 pipeline supervisor._commit_ok 经 command 通道完成(source="ai")
```

治理器(✅ 实装,§4/5/6 三门收口一处):
```python
# ai_governor.py — source='ai' 命令治理器(无状态;阈值/moderator 注入)
governor.guard(spec, args, actor) -> GuardDecision   # 违规 raise ZeError
#   safety(AI-04):   moderator(text)->bool 钩,拦截 raise ai_safety_blocked;缺省放行
#   cost(AI-06):     estimate_cost > per_call_cap_usd → raise ai_cost_exceeded
#   approval(AI-05): cost≥approval_threshold_usd 或 spec.ai_high_risk → GuardDecision(require_confirm=True)
# bootstrap 挂 CommandRegistry(ai_governor=governor);未启用=None,dispatch 不变
```
仍待补(⏳,policy/output):output 过滤、moderator 规则库、日累计 ledger(见 §11 ⏳ 表)。

## 9. audit gates

注册到 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md) 的 `GATE-AI-*`(AI-02/03 及 AI-04/05/06 的**机制层已实装**于 `AiGovernor`;AI-01 及 04/05/06 的 **policy 侧**待项目注入):

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-AI-01` | provenance_required_when_source_ai | source='ai' 的 entity 写入必须带非空 `AiProvenance`(至少 actor+model)| runtime(机制已有,待 policy 定必填字段后落地)|
| `GATE-AI-02` | ai_op_respects_version_cas | source='ai' 提交必传 `expected_version`,冲突→error marker 不覆盖 | runtime(对既有 `_commit_ok` 行为的断言,可即实施)|
| `GATE-AI-03` | ai_provenance_round_trip | `AiProvenance` 经 entity to_dict/from_dict 无损还原 | runtime(机制已有,可即实施)|
| `GATE-AI-04` | ai_io_safety_filter_applied | source='ai' 的 input/output 落 entity 前过 safety filter | runtime(input moderator 钩已实装;output + 规则库 policy 侧待补)|
| `GATE-AI-05` | high_risk_needs_approval | high-risk command 的 source='ai' 触发须经人审才 dispatch | runtime(两段确认机制已实装;阈值/high-risk 定义=policy)|
| `GATE-AI-06` | cost_cap_enforced | 单次 `cost_usd` ≤ per-call cap;日累计 ≤ daily cap,超则拒/熔断 | runtime(单次 cap 机制已实装;cap 值/日累计=policy)|
| `GATE-AI-07` | ai_run_phase_valid | AI 长任务 `EntityRunStatus.phase ∈ RUN_PHASES` | runtime(机制已有;若加 awaiting_approval 需先扩 RUN_PHASES)|

## 10. 红线规则 + 反模式 + 开放策略问题

红线:
- **AI 不能开后门** —— 必须走 command/operation/CAS/undo 同一套,禁止给 source='ai' 单独的直写 entity 路径。
- **AI 提交必走 CAS** —— `_commit_ok` 必传 `expected_version`;冲突 → error marker,**绝不覆盖**并发写。
- **provenance 不可伪造/事后补** —— 写入时绑定字段,不在事后扫描补 provenance。
- **成本/审批是拒绝点不是记录点** —— cost/approval 闸实装后必须能**阻止** dispatch,不能只记日志放行。

反模式:
- **AP-AI-1** 给 AI 单开一条绕过 command 的"快速写入" → 失审计 + 撞不上 CAS。
- **AP-AI-2** AI 提交不带 provenance,事后再补 → 字段级溯源失真。
- **AP-AI-3** safety/approval/cost 做成"只告警不拦截" → 形同虚设。
- **AP-AI-4** 把成本限额另造一套引擎,不复用 06-resource-budget → 两套预算逻辑漂移。

### 开放策略问题(本轮**不默选**,留实际项目接入时由 owner 定)

1. **何为 "high-risk"?** 硬删 / 级联删 / 批量改 / 跨 skill 调用 / 改 read_only? 按 command 显式标 `requires_approval`,还是按 patch 影响面自动判定?
2. **Approval 默认姿态**:opt-in(默认放行,按 command 开)还是 opt-out(默认需审)?
3. **默认 cost caps**:per-call USD 上限、daily USD/token 上限的默认值? 是否分 model 分档? 超日额是"拒新调用"还是"熔断整个 source='ai' 通道"?
4. **Provenance 必填字段集**:`GATE-AI-01` 强制时 actor+model 够不够,还是 trace_id / cost_usd / prompt_template_id 也必填? 缺失 hard-fail 还是 warn?
5. **Safety filter 判定边界**:命中是拒绝整个 operation 还是脱敏后放行? 规则库由框架还是项目维护?
6. **日累计归属与持久化粒度**:per-user / per-skill / per-doc? 内存 vs 落盘(关系到跨 session 熔断是否生效)。
7. **熔断恢复**:日额熔断后人工解除还是按日自动重置?

## 11. 当前实装状态

### ✅ 已实装(本叶子固化为规则,机制在别处)

| 项 | 既有机制 |
|---|---|
| source='ai' 合法且受校验 | [operation.py](../02-operation/operation.py) `SOURCES` + `new_operation()`;`GATE-OP-03` |
| command 可拒 source='ai' | [01-command](../01-command/) `sources_allowed`;`GATE-CMD-05` |
| AI 写入字段级可溯源 | [entity.py](../../01-data-structure/02-entity/entity.py) `AiProvenance` + `_provenance[field]` + `update(ai_provenance=)` + `provenance()` + 序列化往返 |
| AI 提交走 CAS、不覆盖并发写 | [supervisor.py](../03-pipeline/supervisor.py) `_commit_ok(source="ai", expected_version=...)`;冲突→error marker;`GATE-ENT-01` |
| AI 占位 entity 用 system source | [supervisor.py](../03-pipeline/supervisor.py) 占位 `source="system"` |
| 长任务有相位 + 可取消 | [run_status.py](../../_protocol/run_status.py) `EntityRunStatus(pending/running/done/error)` + `CancelledError`/`cancel_event` |
| AI op 可被 undo | 复用 [04-history-rollback](../../02-data-flow/04-history-rollback/) undo 通道(source 无差别)|
| source='ai' 命令过治理器(safety/cost/approval 三门机制)| [ai_governor.py](ai_governor.py) `AiGovernor.guard()`;[bootstrap](../../../sdk/bootstrap.py) 挂 `CommandRegistry(ai_governor=)`;`GATE-AI-04/05/06` 机制层 |
| 高成本/高风险 AI 命令两段确认 | `guard()→require_confirm`→`pending_confirmation`→网页 nonce→[execute_confirmation](../../../sdk/persistence.py);MCP 无 nonce 只 pending |

### ⏳ 待补(policy 挂点 / 跨叶子依赖 —— 机制已在,缺规则/额度/output)

| 项 | 依赖 |
|---|---|
| provenance 完整性强制(`GATE-AI-01`)| policy:必填字段集 + 强制层级(entity.update or command dispatch)|
| safety **output** 过滤 + moderator 规则库(`GATE-AI-04` policy 侧)| input 钩机制已实装;output(结果落 entity 前)未做;`moderator(text)->bool` 规则库由 mod `ai_governor_register.py` 注入、缺省放行;命中处置=policy |
| approval 阈值 / high-risk 划线(`GATE-AI-05` policy 侧)| 两段确认机制已实装;`approval_threshold_usd` / `spec.ai_high_risk` 具体值=policy |
| cost **日累计 / 熔断**(`GATE-AI-06` 日额侧)| 单次上限已实装;日累计需持久 ledger + 与 [06-resource-budget](../../02-data-flow/06-resource-budget/) `CapacityGate` 对齐;额度=policy |

### 原则

- **AI 是 actor 不是通道**:复用 command/operation/CAS/undo,绝不开后门 —— 这是本叶子的根线。
- **机制在 core,policy 随项目注入**:safety/cost/approval 三门机制已由 `AiGovernor` 实装(§4/5/6);剩下的是 policy 挂点(moderator 规则库 / approval 阈值 / high-risk 划线 / 日累计额度)+ output 过滤,真实工具接入 AI 时经 04-ops config / 可选 mod `ai_governor_register.py` 注入、按 §10 补齐,core 不预置、不默选。
- **成本=容量**:cost control 复用 06-resource-budget,不另造预算引擎。

## 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [02-operation](../02-operation/) | source 四态、Operation 同构、可 undo |
| [02-entity](../../01-data-structure/02-entity/) | `AiProvenance` 定义 + 字段级挂载 + `GATE-ENT-01` CAS |
| [03-pipeline](../03-pipeline/) | `_commit_ok` source='ai' 提交 + CAS + EntityRunStatus + 取消 |
| [01-command](../01-command/) | `sources_allowed` / `GATE-CMD-05` ACL —— approval/high-risk 标志挂载点 |
| [05-concurrency](../../02-data-flow/05-concurrency/) | 限流/排队 —— 与 AI API 限流协同 |
| [06-resource-budget](../../02-data-flow/06-resource-budget/) | cost ceiling 与 `CapacityGate`/`ResourceContract` 对齐 |
| [_protocol/CHANGE-FLOW.md](../../_protocol/CHANGE-FLOW.md) | 全链路时序中 source='ai' 的切面 |
| [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md) | 注册 `GATE-AI-*` |
