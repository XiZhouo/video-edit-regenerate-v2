# Cross-Skill Call — A skill 调 B skill 的 command

> **本 SPEC 是规则骨架**:定清问题、契约形状、红线;**关键协议(寻址 / 鉴权 / 传输层)是开放设计决策,待 ZE 系列协同顶层拍板**(见 §6)。本轮**不发明 transport、不写 impl**。

## 1. 这是什么

一个 ZE skill(A)需要调用另一个 ZE skill(B)注册的 command —— 例如出图工具调"背景生成"skill 的 `bg.generate`,或关卡审计调"物体库"skill 的 `object.lookup`。本叶子定义**跨 skill 调用的契约**:怎么寻址 B、调用同步还是异步、超时、错误怎么传回、trace-id 怎么跨 skill 串成一条链。

> 业务白话:业务同事在 A 工具里点一个按钮,背后其实要让 B 工具干一段活再把结果拿回来。框架要保证:A 不必把 B 的代码抄一份、调用失败 A 能知道为什么、整条跨工具的调用在日志里能串起来排查。

### 为什么是独立叶子

| 问题 | 原因 |
|---|---|
| **跨 skill 是进程/服务边界** | 同 skill 内调 command 走 [01-command](../01-command/) registry 即可;跨 skill 是另一个进程/端口/部署,必须有显式传输契约,不能假装是本地调用 |
| **错误语义要跨边界保真** | B 的 `ZeError(code, message)` 要能完整传回 A,不能被 transport 吞成"500" |
| **trace 必须串联** | A 的 trace_id 要带到 B、B 的子操作挂在同一 trace 下,否则跨工具问题无法排查 |
| **鉴权是新面** | A 以"谁"的身份调 B? 复用 [03-authentication](../../04-user-facing/03-authentication/) 的 token,还是 skill 间专用凭证? —— 这是 §6 开放决策 |

## 2. 契约形状(规则,impl 待定)

调用方(A)看到的接口形状(签名草图,**非实装**):

```python
def call_skill(
    skill_id: str,          # 被调 skill(如 "ZE-seek-find-background-generator")
    command_id: str,        # B 注册的 command(如 "bg.generate")
    payload: dict,
    *,
    actor: Actor,           # 以谁的身份调(鉴权 → §6)
    mode: str = "sync",     # "sync" | "async"
    timeout_sec: float = 30,
    trace_id: str = "",     # 不传则继承当前 trace,串成跨 skill 链
) -> dict:                  # B command 的结果(或 async 的 handle)
    raise NotImplementedError("待实施: 传输层未定，见 §6")
```

规则(无论传输层怎么选都要满足):
- **同步 vs 异步**:`sync` 阻塞拿结果(短任务);`async` 立即返 handle,B 跑完经回调/轮询通知(长任务,对齐 [03-pipeline](../03-pipeline/) 的 PendingOperation 思路)。
- **超时**:`sync` 必带 `timeout_sec`,超时 A 收到 `cross_skill_timeout`,不挂死。
- **错误传播**:B 的 `ZeError(code, message)` 原样传回 A(code 保真,不被 transport 压成 HTTP status);传输层失败(连不上/超时)用专门的 `cross_skill_*` code 区分于"B 业务报错"。
- **trace 串联**:A 的 `trace_id` 带到 B;B 产生的 Operation 挂同一 trace_id → 全链路在 OpLog/日志里可串。
- **B 侧是 command,不是后门**:B 只暴露已注册的 command(走 B 的 `sources_allowed` ACL),A 不能调 B 未导出的内部函数。

## 3. 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [01-command](../01-command/) | B 侧被调的是注册 command;跨 skill 调用是 command dispatch 的远程形态 |
| [02-operation](../02-operation/) | B 执行产生的 Operation 带 A 传来的 trace_id;可能用 `source="sync"` 标记跨 skill 来源(待定) |
| [03-authentication](../../04-user-facing/03-authentication/) | 跨 skill 鉴权可能复用 token/Actor(§6 决策) |
| [03-pipeline](../03-pipeline/) | async 跨 skill 调用与管线长任务模型对齐 |
| [_protocol/CHANGE-FLOW.md](../../_protocol/CHANGE-FLOW.md) | trace-id 跨 skill 串联是全链路追踪的延伸 |

## 4. 红线规则 + 反模式

红线:
- **不假装本地**:跨 skill 调用必须显式声明(`call_skill`),不能 import B 的 Python 模块直接调 —— 那会把两个 skill 编译期耦死。
- **错误 code 保真**:B 的业务错误 code 必须能传回 A;传输失败与业务失败用不同 code 族区分。
- **只调导出 command**:A 不能绕过 B 的 command registry/ACL 调内部函数。
- **trace 不断**:跨 skill 调用必须延续 trace_id。

反模式:
- **AP-XSK-1** A 直接 `import` B 的代码 → 编译期耦合,B 一改 A 就崩,违背 skill 独立。
- **AP-XSK-2** transport 把 B 的 `ZeError` 吞成裸 500 → A 无法区分"B 拒绝"和"网断"。
- **AP-XSK-3** 跨 skill 调用不带 trace → 跨工具问题无法排查。
- **AP-XSK-4** sync 调用无超时 → B 挂了 A 跟着挂死。

## 5. audit gates

`GATE-XSK-*`(前缀**待定稿后注册**;注意 `GATE-CC` 已被 [05-concurrency](../../02-data-flow/05-concurrency/) 占用,本叶子不能用 CC)。候选规则:
- `error_code_preserved_across_skill` —— B 的 ZeError code 传回 A 不丢。
- `trace_id_propagated` —— 跨 skill 调用延续 trace_id。
- `sync_call_has_timeout` —— sync 模式必带超时。
- `only_registered_command_callable` —— A 只能调 B 已注册 command。

均待 §6 传输层定稿后再注册 + 实施。

## 6. 当前实装状态

### ⏳ 全待补(规则已立,impl + 关键协议决策未定)

| 项 | 依赖 / 开放决策 |
|---|---|
| **传输层** | HTTP(REST 跨端口)? CLI(`ze_command.py run` 跨目录)? 还是共享 registry? —— **需 ZE 系列协同顶层决策** |
| **skill 寻址** | A 怎么知道 B 在哪(端口/URL)? 读 [ports.json](../../../../ZE-skill-deploy/server-deploy/PORTS.md)? 服务发现? 静态配置? |
| **跨 skill 鉴权** | A 以谁身份调 B? 复用 [03-authentication](../../04-user-facing/03-authentication/) token? skill 间专用凭证? 还是内网信任(裸调)? |
| **async 回调机制** | B 跑完怎么通知 A? webhook? 轮询? 复用 SSE? |
| `call_skill` 实现 + `GATE-XSK-*` | 上述定稿后 |

### 原则

- **先有规则,设计待生态**:本 SPEC 锁住"跨 skill 调用必须满足什么"(错误保真 / trace 串联 / 只调 command / 不假装本地),但**故意不锁传输层** —— 那是 ZE 系列协同的顶层决策,等真有跨 skill 需求的实际场景驱动再定,避免现在凭空发明一套用不上的 RPC。
- **不假装本地是根线**:无论传输层怎么选,跨 skill 永远是显式的 `call_skill`,不是 `import`。
