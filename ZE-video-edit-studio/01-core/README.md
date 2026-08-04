# 01-core — 框架抽象 + 默认实现

> ## ⛔ 动手前必读：这一层在下游 skill 里是只读的
>
> 如果你（人或 AI）正在**某个下游 skill**（不是 ZE-skill-build 本身）里读到这个文件：
> 本目录及 `sdk/`、`webapp/src`（除 `product/`）、`03-css-rules/`、`05-i18n/`、`04-ops/`、toolchain 脚本
> 都是从 build-v3 **整层复制**来的受管拷贝。**直接在这里改 = 白改**：下一次任何人跑
> `skill_sync --apply` 会整层替换、静默覆盖你的改动——commit 过、bump 过 lock、门禁全绿都拦不住
> （2026-07-02 真实事故：batch-image-foundry 三笔 gallery 控制区定制被一次 sync 全部抹掉）。
>
> 每次修改前先判断落点：
> 1. **通用能力 / 框架修复** → 改 `ZE-skill-build` 上游对应文件，bump 契约版本，`skill_sync --apply` 下发。
> 2. **本 skill 业务策略** → 落 `02-mod/` 或 `webapp/src/product/`（sync 不碰的层）。
> 3. **紧急热修了本层** → 同一工作周期内必须回流上游并 sync 对齐，不许留「下游领先上游」。
> 4. **跑 sync 之前** → 先 `git log <目标skill>/01-core` 查有没有未回流的本地提交，有就先回流再 sync。

ZE 工程的**通用底层**，所有 ZE skill 共享。复制到新 skill 时整体复制，升级时整体替换。

mod 不持有 core 任何完整副本，**只通过引用**取用 / override / 具体化。

## 阅读顺序（4 个子树）

1. [01-data-structure/](./01-data-structure/) — 数据是什么（Document / Entity / Asset / 字段类型 / 容量限制）
2. [02-data-flow/](./02-data-flow/) — 数据怎么变（实时同步 / 冲突解决 / 离线优先 / 历史回滚 / 并发 / 重资源预算）
3. [03-commands-operations/](./03-commands-operations/) — 谁让数据变（Command 注册 / Operation 日志 / Pipeline / 跨 skill / AI 协作）
4. [04-user-facing/](./04-user-facing/) — 人怎么用（界面布局 / 交互体验 / 身份验证）
