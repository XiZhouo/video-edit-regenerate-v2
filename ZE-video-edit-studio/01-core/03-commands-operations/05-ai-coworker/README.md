# 05-ai-coworker — AI 协作

> 一句话：AI 作为 actor 的安全约束。

AI Provenance (谁 / 哪个模型 / 什么 prompt / 多少 token / 多少 cost) / AI Safety (input/output 过滤) / Human Approval (高风险动作需人审) / Cost Control (单次/日额上限)。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — **已写(规则态)**:固化已有机制(provenance / source ACL / CAS / 取消) + 登记 3 道未来闸(安全过滤 / 人审 / 成本) + 7 个 `GATE-AI-*` 注册 + 开放策略问题
- impl 待实际项目补(本叶子无独立 .py;机制散在 02-operation / 02-entity / 03-pipeline)

## 怎么用

mod 作者给 source='ai' 的命令接治理，分三处声明：

```python
# ① 02-mod/05-code/commands_register.py — 在 CommandSpec 上声明成本/风险
CommandSpec(id="object.generate", label="生成物体",
            ai_cost_usd=0.05,     # 单次成本估算(USD), AiGovernor 据此卡上限/审批阈值
            ai_high_risk=False,   # True → source='ai' 触发强制走两段式确认
            ...)

# ② 02-mod/05-code/ai_governor_register.py(可选) — 安全过滤钩
def moderator(text: str) -> bool:  # True=放行 / False=拦截 → ai_safety_blocked
    ...

# ③ config(04-ops) — 打开治理并定阈值
#    ze.ai.governance_enabled=true / ze.ai.require_provenance
#    ze.ai.per_call_cap_usd / ze.ai.approval_threshold_usd(0 = 该项不限)
```

治理开启后，source='ai' 的 dispatch args 必须带非空 `provenance` / `ai_provenance`（缺则报 `ai_provenance_required`）；超单次上限报 `ai_cost_exceeded`；高风险或估算成本达审批阈值时先返回两段式确认，confirm 后才执行。字段级溯源用 `entity.update(patches, ai_provenance={field: AiProvenance(actor=..., model=...)})` 写入、`entity.provenance(field)` 读出。完整规则见 [SPEC.md](./SPEC.md)。
