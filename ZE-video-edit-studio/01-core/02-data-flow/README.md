# 01-core/02-data-flow — 数据流

数据怎么变 + 多人/多 actor 协作时怎么保持一致。

## 8 个叶子

1. [01-realtime-sync/](./01-realtime-sync/) — 实时同步（A 改完 B < 100ms 看到 / WebSocket / 增量 patch）
2. [02-conflict-resolution/](./02-conflict-resolution/) — 冲突解决（Advisory Lock / CAS / reject / merge 策略）
3. [03-offline-first/](./03-offline-first/) — 离线优先（离线队列 / CRDT / 上线后 reconcile）
4. [04-history-rollback/](./04-history-rollback/) — 历史与回滚（字段历史 / 一键回滚 / Checkpoint / undo/redo）
5. [05-concurrency/](./05-concurrency/) — 并发控制（同一资源多并发写入的隔离 / 限流）
6. [06-resource-budget/](./06-resource-budget/) — 重资源预算（架构合同 + 唯一入口闸门 + 乘法审计 + 运行时自证）
7. [07-persistence/](./07-persistence/) — 写入持久化（提交协调、耐久性、恢复与本地存储权威）
8. [08-local-update/](./08-local-update/) — 本地软件更新（双验签、版本目录、原子激活、健康检查与自动回切）
