# 03-offline-first — 离线优先

> 一句话：断网仍可工作。

本地离线队列 / 7 种 CRDT 类型 / 上线后 reconcile 协议。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — **已写(显式 deferred)**:说明范围 + 为何暂不做(内网单机不需要 + 强耦合 02-conflict-resolution 的 CRDT)+ 将来要做的切入点
- impl 全 deferred(与 conflict-resolution CRDT 一起做)

## 怎么用

mod 作者不需要为本叶子声明或配置任何东西：框架的运行假设是内网在线，断网即不可用是可接受行为。要守的只有一条——不要在 mod 里自造本地离线队列 / 断网缓存层，离线合并归框架层（依赖 [02-conflict-resolution](../02-conflict-resolution/) 的 CRDT 引擎和 [01-realtime-sync](../01-realtime-sync/) 的 `since_seq` 重放），mod 自造会与之分叉。业务真有离线场景时，按 SPEC §3 的切入点提给框架层再接入。完整规则见 [SPEC.md](./SPEC.md)。
