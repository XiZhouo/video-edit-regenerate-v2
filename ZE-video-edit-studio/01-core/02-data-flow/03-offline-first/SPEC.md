# Offline-First — 离线优先(显式 deferred)

> **本叶子规则 deferred,未设计。** 本 SPEC 诚实说明范围、为什么暂不做、以及一旦要做的依赖与切入点 —— 而不是假装有一套规则。

## 1. 这是什么(范围)

让 ZE 工具**断网仍可工作**:本地缓存 Document、离线期间的编辑进本地队列、重新上线后与服务端 reconcile(用 CRDT 等策略自动合并并发改动)。README 一句话:本地离线队列 / CRDT 类型 / 上线后 reconcile 协议。

## 2. 为什么暂不做(deferred 理由)

| 理由 | 说明 |
|---|---|
| **目标场景不需要** | ZE 工具是**内网单机**部署(amber 等),业务同事在内网浏览器用;不是公网移动端、不是弱网现场。断网即不可用是可接受的运行假设。 |
| **强耦合另一个 deferred 叶子** | 离线 reconcile 的核心是 CRDT 自动合并,而 [02-conflict-resolution](../02-conflict-resolution/) 的 CRDT 策略**本身就是 `NotImplementedError` 占位**(412 行的大件,刻意缓做)。两者必须一起做,单独启动 offline-first 没有合并引擎可用。 |
| **复杂度 vs 收益失衡** | 本地队列 + 7 种 CRDT + reconcile 协议是重投入;在内网单机场景下收益接近零。判定 YAGNI。 |

## 3. 一旦要做的依赖与切入点

若将来出现真实的离线场景(如外勤/弱网现场工具),按此切入:
- **先做 [02-conflict-resolution](../02-conflict-resolution/) 的 CRDT 引擎**(LWW-Register / G-Counter / OR-Set / RGA 等),它是 reconcile 的基础。
- **本地 Document 缓存 + 离线 OpLog 队列**:复用 [02-operation](../../03-commands-operations/02-operation/) 的 OpLog(已支持落盘),离线期 append 到本地队列,上线后按序重放 + 冲突走 CRDT。
- **reconcile 协议**:与 [01-realtime-sync](../01-realtime-sync/) 的 `since_seq` replay 机制对接(离线期错过的 seq 上线后补)。
- **与 [05-concurrency](../05-concurrency/) 的版本/CAS 协同**:重放时的版本冲突走 conflict-resolution。

## 4. 当前实装状态

### ⏳ 全部 deferred(未设计)

| 项 | 依赖 |
|---|---|
| 本地离线队列 | [02-operation](../../03-commands-operations/02-operation/) OpLog 落盘(已有)+ 本地缓存层(未做)|
| CRDT 合并类型 | [02-conflict-resolution](../02-conflict-resolution/) CRDT 引擎(也 deferred)|
| 上线 reconcile 协议 | [01-realtime-sync](../01-realtime-sync/) `since_seq` replay + 上述两项 |

### 原则

- **诚实标 deferred,不假装有规则**:本叶子不写投机性的 CRDT 设计 —— 内网单机场景下那是空中楼阁。规则等真实离线需求 + conflict-resolution CRDT 一起落地时再写。
- **切入点已记**:上面 §3 留好"要做时从哪起",避免将来从零摸索。
