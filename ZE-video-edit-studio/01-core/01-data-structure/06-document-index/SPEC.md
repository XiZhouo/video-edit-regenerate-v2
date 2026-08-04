# DocumentIndex SPEC

`DocumentIndex` is a lightweight persisted index over many Documents.

## Invariants

- An index entry references exactly one Document by `document_id`.
- The index does not embed document entities, operation logs, checkpoints or tasks.
- Archived entries are hidden from `list()` by default and remain available with
  `include_archived=True`.
- `save(path)` writes a single JSON file with schema `ze-document-index`.
- `open(path)` only accepts files with schema `ze-document-index`.

## Entry Shape

```json
{
  "document_id": "level-0450",
  "kind": "seek_find_level",
  "label": "0450",
  "thumbnail_asset_id": "asset1",
  "source_path": "0450",
  "status": "ready",
  "updated_at": 0,
  "meta": {}
}
```

Valid statuses are `empty`, `ready`, `running`, `error`, and `archived`.
