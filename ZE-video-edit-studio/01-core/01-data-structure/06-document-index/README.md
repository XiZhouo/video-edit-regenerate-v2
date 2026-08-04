# DocumentIndex

`DocumentIndex` is the core primitive for workbenches that manage many
documents without merging their data into one large document.

Use it when a product concept is a batch/project of independent documents:
for example, a seek-find object project containing many level documents, or a
background project containing many background documents.

Core owns:

- document references
- labels, thumbnails, status and source paths
- aggregate status for list and batch views
- save/open of the index file

Core does not own:

- entities inside each referenced document
- task logic for a domain
- export protocols such as seek-find `scene/...`

## 怎么用

```python
from sdk import DocumentIndex, DocumentIndexEntry

idx = DocumentIndex(kind="seek_find_project", title="物体项目 A")
idx.add(DocumentIndexEntry(
    document_id="level-0450",      # 一行只引用一个 Document
    kind="seek_find_level",
    label="0450",
    source_path="0450",
    status="ready",                # empty | ready | running | error | archived
))
idx.set_aggregate_status({"ready": 1, "running": 0})
idx.save("project/index.json")     # 单 JSON 文件, schema="ze-document-index"

idx2 = DocumentIndex.open("project/index.json")
rows = idx2.list()                 # 默认隐藏 archived; list(include_archived=True) 全列
```

Index 只存引用 / 标签 / 缩略图 / 状态；每个 Document 自己的 entities / oplog 留在各自 Document 内，不要塞进 index。完整规则见 [SPEC.md](./SPEC.md)。
