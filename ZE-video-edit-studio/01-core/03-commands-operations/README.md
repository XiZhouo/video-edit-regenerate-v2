# 01-core/03-commands-operations — 指令与操作

所有"会改数据"的动作都走这套系统。统一 Command 注册 → 产生 Operation → 落 OpLog。

## 5 个叶子

1. [01-command/](./01-command/) — Command（注册规定 + Registry + Dispatcher）
2. [02-operation/](./02-operation/) — Operation（patches + reverse patches + 来源标记：人/ai/系统）
3. [03-pipeline/](./03-pipeline/) — Pipeline（compose 命令 / 多 stage 编排 / DAG）
4. [04-cross-skill-call/](./04-cross-skill-call/) — 跨 skill 调用（A skill 调 B skill 的 command）
5. [05-ai-coworker/](./05-ai-coworker/) — AI 协作（provenance / safety / approval / cost 控制）
