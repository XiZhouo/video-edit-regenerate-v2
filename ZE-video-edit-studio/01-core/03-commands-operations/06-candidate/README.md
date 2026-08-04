# Candidate Protocol

The candidate protocol models generated outputs that must be adopted before
they become canonical document state.

This is the shared core shape for AI-heavy workflows such as seek-find object
generation: AI writes candidates, then a user/AI command adopts or rejects them.

Core owns:

- `CandidateRecord`
- `__candidate__` entity shape
- adopt/reject operation factories
- status lifecycle: `generated`, `adopted`, `rejected`, `stale`

Core does not own:

- candidate scoring algorithms
- image generation or extraction
- domain-specific asset fields
- UI layout for candidate galleries

## 怎么用

```python
from sdk import (Actor, CANDIDATE_ENTITY_TYPE, new_candidate_record,
                 candidate_adopt_operation, candidate_reject_operation)

# 生成结果先入档为候选, 不直接改目标 entity
rec = new_candidate_record(parent_entity_id="obj1", document_id="level1",
                           kind="object_png", asset_ids=["asset1"], score=0.8)
doc.add(rec.to_entity())              # 落为 Entity(type="__candidate__")

# 采纳: 造标准 Operation(目标 entity 写 adopted_assets 字段 + 候选状态置 adopted)
op = candidate_adopt_operation(
    candidate_entity=doc.get(CANDIDATE_ENTITY_TYPE, rec.candidate_id),
    target_entity=doc.get("object", "obj1"),
    adopted_assets={"object": "asset1"},
    actor=Actor(user_id="u1", source="user"),
)
# 拒绝: 只改候选状态(adopted 后不可 reject)
op2 = candidate_reject_operation(candidate_entity=..., actor=...)
```

本叶子只造 Operation、不应用——拿到 op 后仍走标准 command/operation/OpLog/Document 通道落库。完整规则见 [SPEC.md](./SPEC.md)。
