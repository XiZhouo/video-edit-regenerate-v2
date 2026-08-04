# Candidate Protocol SPEC

Generated outputs enter the document as `Entity(type="__candidate__")`.
They do not update canonical target entities until an adopt operation runs.

## Candidate Record

```json
{
  "candidate_id": "cand1",
  "parent_entity_id": "obj1",
  "document_id": "level1",
  "kind": "object_png",
  "asset_ids": ["asset1"],
  "status": "generated",
  "source_task_id": "task1",
  "score": 0.8,
  "created_at": 0,
  "meta": {}
}
```

## Invariants

- Valid statuses are `generated`, `adopted`, `rejected`, and `stale`.
- Adopt is only allowed from `generated` or `stale`.
- Reject is not allowed after adoption.
- Adopt produces a normal Operation that patches the target entity and marks the
  candidate as adopted.
- Reject produces a normal Operation that only changes candidate status.
- Core does not apply operations; callers still use the standard
  Command/Operation/OpLog/Document path.
