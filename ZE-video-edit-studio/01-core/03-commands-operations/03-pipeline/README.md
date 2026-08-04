# 03-pipeline — Pipeline

> 一句话：**长任务多步协调**。Stage = 一步（带 progress + cancel + retry），Pipeline = 多 Stage 顺序执行 + fan_out + precise retry。Command 给不了"进度可见 + 可取消 + 单步失败重跑"，pipeline 专门做这件事。

## 在框架里的位置

- 上一级：[03-commands-operations/](../) — 指令与操作 5 个叶子
- 同级：[01-command/](../01-command/) / [02-operation/](../02-operation/) / [04-cross-skill-call/](../04-cross-skill-call/) / [05-ai-coworker/](../05-ai-coworker/)
- 跟 command 的关系：stage handler 内部走 command dispatch 做数据变更；本叶子只协调执行流

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（Stage vs Command / fan_out / precise retry / cancel 协议）|
| [stage.py](./stage.py) | StageSpec / StageContext / StageResult / StageRunner / CancelledError |
| [pipeline.py](./pipeline.py) | PipelineStep / PipelineRun / PipelineStepResult / PipelineRunner |
| [work_list.py](./work_list.py) | WorkList / WorkItem 与 artifact 路径白名单 |
| [artifact_scan.py](./artifact_scan.py) | attempt、completion receipt、checksum scan 与 crash repair |
| [stage_plan.py](./stage_plan.py) | StageSpec 的唯一完整、无 callable 元数据序列化：身份、产物、资源、重试/中断/暂停与完整 port bag |
| [supervisor.py](./supervisor.py) | 活管线提交、worker、暂停/恢复与 receipt 接线 |
| [pipeline_authority_codec.py](../../_protocol/pipeline_authority_codec.py) | v2 Start/Control 严格 owned codec；描述符只定义耐久事实，不签 grant |
| [pipeline_authority_reducer.py](../../_protocol/pipeline_authority_reducer.py) | 从完整 fresh OpLog snapshot 纯归约有界权限事实；不读 manifest/resolver/Document |
| [pipeline_authority_commit.py](../../_protocol/pipeline_authority_commit.py) | private reserved-ID sealed factory、pre-store permit、exact FileCommitStore/OpLog append 权限与 durable fresh attestation；不向 worker 导出 |
| [pipeline_delegated_mutation.py](../../_protocol/pipeline_delegated_mutation.py) | core 3.40 dormant delegated request；prepare/consume 都重读 fresh history 并重验原业务 policy，只证明一次请求没有越权，不执行写入 |
| [test_pipeline.py](./test_pipeline.py) | pytest 测试（29 个）|

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `StageSpec` / `StagePlan` — 完整冻结 id、callable、重试/中断/断点、资源、产物与 exact JSON port bag；执行与 `to_meta()` 只读同一份构造时权威
- `StageContext` — progress / label / eta + `set_progress()` + `checkpoint()` + cancel token
- `StageResult` — status (ok / cancelled / error) + output / error / 时间戳
- `StageRunner` — 同步跑 stage，try/except 统一处理 CancelledError / ZeError / Exception，支持外部 `cancel(task_id)`
- `PipelineStep` — stage + fan_out 标志
- `PipelineRun` — id / steps / status (running / ok / partial / failed / cancelled)
- `PipelineStepResult` — fan_out 多 item results + ok_items / failed_items
- `PipelineRunner.run` — 顺序多 step + fan_out 展开 + 失败短路 / continue_on_failure
- `PipelineRunner.retry_failed` — 只重跑失败 item / step，ok/cancelled 保留
- `CancelledError` 异常类
- ZeError 共享（从 entity 模块 import）
- `PipelineSupervisor` 实体级状态：先把占位与启动 Operation 放进同一 `CommandMutation`，提交后才唤醒 worker；只有本次确实发出 `entity.add` 的 item 才签发 `EntityRunStatus.placeholder=true`。该 item 集写入 `run.json`，恢复后继续保持；旧 manifest 缺字段按普通实体处理。前端不得从空 data、名字或文案猜 placeholder，running/error 只装饰目标实体。
- 执行意图深拥有：`StagePlan` 不保留 caller 传入的 `StageSpec` 引用，而是从每个 stage 的构造快照重建自己的副本；运行字段、资源与完整 port bag 从公开属性或 `to_meta()` 读取时都落到同一快照。`WorkItem` 只接受精确 JSON payload，内部保存 canonical UTF-8 bytes；`WorkList` 再深克隆 item 与 identity。原始 caller 容器、公开字段改写、公开 `__dict__` 的 detached view、base dict/list mutator、deepcopy/pickle 与自定义 deepcopy hook 都不能让正常框架读取出现“描述 A、执行 B”。反序列化边界还会先验 exact dict/list，拒绝把 tuple/string/list-of-pairs 静默强转成合法 manifest。
- **同进程边界**：上条防的是普通 alias 与框架公开读取面，不是 hostile Python sandbox。能直接调用 `object.__getattribute__` 改真实 private storage 的同进程代码，或可变 handler/code identity，不在本批隔离承诺内；本目录 SPEC §11 的 handler identity 治理，以及 Batch 3/5 的 sealed factory、grant 与消费边界必须继续绑定，文档不得把当前快照外推成敌对同进程安全能力。
- 冷恢复身份绑定：业务若把输入/adapter/关键配置写入 `payload.seed_data`，可用 `payload.recovery_identity_fields` 精确列出其中不可换绑的顶层字段。`WorkItem` 在启动与反序列化时拒绝空、重复、缺失字段；stage args、manifest 与 `to_dict()` 每次都从 canonical 构造快照解出 detached JSON。Supervisor 在首次起跑和任何 marker 修复/worker 恢复之前，从该快照把 manifest seed 与已经提交的 Document entity 做严格 JSON 比较。不一致就拒绝该 run，不能靠构造后缩减字段集合、或同时篡改 payload 与 seed，把另一份输入挂到旧 provenance。resolver 必须把字段列表本身纳入自己的闭集校验；未声明的旧 manifest 仅保持兼容，不能据此声称已有耐久 provenance 绑定。
- 拒绝必须可观察：manifest 只经 owned real directory fd + `O_NOFOLLOW` 打开 singly-linked regular `run.json`，并在有界读取前后复核同一 inode/size/mtime；目录 symlink 直接不进入信任域。安全目录下的 manifest 若 JSON/shape 损坏、run id 与目录不等、resolver 不认识或耐久身份不匹配，单坏 run 不拖垮其它恢复，但也不再静默消失。Supervisor 从仍存在的 run 目录重建一个只读 `status=error,recovery_required=true` 快照，只暴露固定 `recovery_error.code`，不泄漏 parser 文本/路径/stack，也不提供可能重放副作用的 resume/retry。它是 repair 信号，不冒充已经自动修复；无法可信定位 entity 时不会用不可信 manifest 给任意实体贴错误态。
- 诚实进度：未调用 `ctx.set_progress()` 时为不确定态；完成项/阶段计数与工作量百分比分开，完成态固定 100%。
- Pipeline durable authority Batch 3：严格 v2 descriptor 继续绑定 WorkList、StagePlan、placeholder/初始闭包、root command/policy/schema digest、worker 与和 WorkList 完全相等的 delegated targets；v1 永久只算 carrier。3.39 新增 active business-handler root snapshot 与 private reserved-ID Start/Control factory，public Operation/new_operation 仍拒绝 v2。受保护 `prepare_start` 不接受 caller 预造 Start，Start 必须是整笔 commit 首项，`initial_operation_ids` 必须与实际 tuple 逐项同序相等，root actor/source/document 也覆盖全部新 placeholder。CommitCoordinator 在 store 前以 complete prospective history 证明闭包，并只在 receipt committed+双 durable、history authority 已收口后，从 exact FileCommitStore/OpLog fresh 读取并再次归约；任何 raw/unsealed/foreign/reused writer、copy/pickle/fork、head 漂移或 fresh failure 都不产生证明。成功 attestation 只留在 private registry，旧证明会在新 durable 结果可能写入时先撤销；它不进入 receipt/event/SDK，更不是 worker grant。
- Pipeline durable authority Batch 4（core 3.40）：新 seam 只准备、消费一张 dormant request。prepare 与 consume 各自都从 exact `FileCommitStore`/OpLog 做一次 fresh reduce，并重验同一 Document、sealed CommandRegistry 与 canonical history graph；不依赖 prepare 时的旧快照。请求必须同时符合耐久 `item→entity`、stage allowlist、字段顶层 root、lifecycle、实体对象/版本、当前 cardinality 与实际 worker actor/source；当前业务 root registration digest 和 Document policy digest 也会每次重证。`entity.*` 通用命令无论作为 root 还是本次 primary Operation id 都被拒绝，不能借 `entity.update` 扩权。
- Batch 4 request 是一次性、不可 copy/deepcopy/pickle、fork 后失效的进程内对象；事实被发行时完整性绑定，公开投影始终 `execution_authority=false`。consume 无论成败都不能再用。成功只表示“此刻这一次请求的范围未越权”；它不 mutate Document、不 dispatch command、不签 grant、不创建或启动 worker、`DeferredAction`、manifest、run 或 v2 Control，也不实际调用 command handler。动态 creation、hot/cold lifecycle 和真实受保护执行都留给 Batch 5。
- Batch 3–4 的 lifecycle 边界必须说清：protected integration 已把真实注册业务命令接到 **sealed Start** 的完整耐久纵向链，但提交成功后明确没有 action、manifest、run 注册或 worker；默认 item/shot demo 因尚无 mutation policy，仍只走 legacy 兼容链。Control factory、codec、append 与 reducer 已可被框架安全持久化并有真实故障测试，但现有 `pipeline.pause/resume/cancel/retry/...` 仍沿用 legacy lifecycle Operation/DeferredAction，尚未消费 v2 Control。它们不能被未来冷恢复猜成 Control；Batch 5 接 control reduce/grant registry 时必须对这段过渡历史 fail closed 或显式迁移。当前没有 cold grant issuer；只有 unprotected/legacy Start 保留 after-commit worker，protected v2 Start 在 grant 出现前不可执行。

**⏳ 待补**（真有跨叶子依赖）

- 并发 fan_out（thread pool / async）— caller 用 `PipelineRunner` 子类自定义；本叶子默认同步
- progress 节流 — 等 [01-realtime-sync/](../../02-data-flow/01-realtime-sync/) 启动后接入 broadcast
- pause / resume（中间状态持久化）— 等 [02-data-flow/](../../02-data-flow/)
- DAG（非线性依赖）— 顺序步骤够用；真有业务需求再扩
- 跨 skill stage — 等 [04-cross-skill-call/](../04-cross-skill-call/)

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

> 操作台接线（1.28.0，配方 COOKBOOK R16）：mod 的 start 命令声明 `params_schema` 并在
> 在 command producer 中调用 `supervisor.prepare_start(..., start_command_id=<命令id>)`，
> 并返回它产生的 `CommandMutation` → pipeline 编辑器经
> `view_state.startCommandId` 渲出「填参数 + Run」操作台；外部运行时缺位时 start 命令返回
> `op.error={code:'requires_runtime', message, missing[], hint}`（本叶子 SPEC §4.3 双通道约定）。
> 禁止 producer 直接调用 `supervisor.start()`：HTTP dispatch 已持有同一个 CommitCoordinator，
> 直接 start 会形成嵌套提交；提交后控制动作必须使用显式 `DeferredAction`。


### 场景 1：单 stage（带 progress + cancel）

```python
from stage import StageSpec, StageRunner

def generate_image(ctx, args):
    for i in range(10):
        ctx.checkpoint()                              # 检 cancel
        # 调外部 AI API
        time.sleep(2)
        ctx.set_progress((i+1) / 10, label=f"生成 {i+1}/10")
    return {"image_url": "https://..."}

spec = StageSpec(id="gen-img", label="生图", run=generate_image)
runner = StageRunner()

# 主线程: 跑
result = runner.run(spec, {"prompt": "..."}, on_progress=lambda ctx: print(ctx.progress))

# 另一线程 / async: 取消
runner.cancel(result.task_id)
```

### 场景 2：pipeline 多步（含 fan_out 批量）

```python
from pipeline import PipelineStep, PipelineRunner
from stage import StageSpec

# Step 1: 抽取关键词 (单步)
extract_kw = StageSpec(
    id="extract",
    label="抽取关键词",
    run=lambda ctx, args: {"keywords": ai_extract(args["text"])},
)

# Step 2: 为每个关键词生成图 (fan_out)
gen_per_kw = StageSpec(
    id="gen",
    label="按关键词生图",
    run=lambda ctx, args: {"url": ai_gen_image(args["item"])},  # args["item"] = 一个关键词
)

steps = [
    PipelineStep(extract_kw),
    PipelineStep(gen_per_kw, fan_out=True),    # 对 items 每个跑一次
]

runner = PipelineRunner()
run = runner.run(steps, {"text": "...", "items": ["概念A", "概念B", "概念C"]})

# 假设生 B 失败了
if run.status == "partial":
    run = runner.retry_failed(run, steps, args)   # 只重跑 B
```

### 场景 3：stage handler 内部走 command 改 ZE 数据

```python
def create_entities(ctx, args):
    for i, row in enumerate(args["rows"]):
        ctx.checkpoint()
        # 通过 command dispatch 改数据 (产生 Operation 落 OpLog)
        cmd_registry.dispatch(
            "entity.add",
            {"type": "entry", "data": row},
            actor=args["actor"], source="user", doc=args["doc"], op_log=args["op_log"],
        )
        ctx.set_progress((i+1) / len(args["rows"]))
    return {"count": len(args["rows"])}
```

## 设计要点

- **Stage handler 必须周期性 `checkpoint()`** — 不调就没法 cancel；颗粒度由业务方决定（每 item / 每 N 秒 / 每 sub-step）
- **Stage 副作用走 command** — 改 ZE 数据走 `cmd_registry.dispatch`，不在 stage 里直调 doc.add（同 AP-C1）
- **fan_out 顺序执行** — 默认同步顺序；要并发，caller 包 thread / 子类覆盖
- **precise retry** — `retry_failed` 只重跑失败的，ok 不动；Supervisor
  在自动重试、手动 retry、rerun/run-from 和崩溃恢复中都强制
  `retry_safe`，遇显式 `False` 以 `pipeline_replay_unsafe` 拒绝且不改 marker。
  检查只覆盖实际重跑的 suffix，不误拦已完成前缀。`retry_safe`
  默认 `True` 保持纯本地旧 stage 兼容；外部/不幂等 stage 必须显式声明 `False`。
- **canonical history 失败不丢线程** — Supervisor 只按 typed
  `HistoryAuthorityError.outcome_unknown` 分类：post-write unknown 持久化为
  `commit_repair_required` 并在对账修复前阻断 replay；pre-write mismatch 标记
  `reload_required`，当前 Context 不重试，只有新 Context recover 后才允许一次显式 retry，
  且仍受 `retry_safe` 约束。`recover()` 自身不会自动执行。
- **receipt 才是完成证明** — stage 收到的 `_artifacts` 是本次 UUID attempt
  staging 路径；Document commit 明确成功后，Supervisor 才写 committed
  checkpoint、原子 promote，并用含 `commit_kind/commit_id/operation_ids` 与
  artifact `path/sha256/size` 的 v1 receipt 标 done。裸最终文件、旧 `ok`
  marker、空/半 receipt 都不能证明 done；已存在但非法的 completion 证据会
  fail closed 为 repair-required，不会当 pending 盲重放。多 stage 的 receipt union 必须覆盖
  WorkItem 全部 artifact。
- **rerun 是 artifact CAS** — attempt 记录开始时 final 的 hash；只能将那个旧版原子替换为新版。
  并发胜者或篡改使 final 偏离基线时报 `artifact_target_conflict`，不覆盖。
- **耐久错误必须诚实** — checkpoint/done/final promote 的目录 fsync 只对明确“不支持”的 errno
  降级；EIO 等真实错误进 `artifact_durability_unknown` / completion repair，不伪报成功。
- **progress callback 不能阻塞 / 抛错** — 抛错被吞掉，但慢 callback 会拖累 stage

## 跑测试

```bash
cd 01-core/03-commands-operations/03-pipeline/
python -m pytest test_pipeline.py -v
```
