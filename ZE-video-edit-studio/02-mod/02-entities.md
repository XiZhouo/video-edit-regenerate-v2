# Entity 类型清单

## 1. entity_type 表

| name | 说明 | collaboration_unit |
|---|---|---|
| `project` | 画布规格与全局上下文 | true |
| `asset` | 视频、图片及处理后文件引用 | true |
| `clip` | 完整时间线中的视频图层 | true |
| `annotation` | 带时段的画面引导（兼容内部名） | true |
| `guide_keyframe` | 矩形或图片在一个关键画面的空间状态 | true |
| `reference_frame` | 用户选择的参考画面时刻 | true |
| `send_profile` | 当前生成配置 | true |
| `send_job` | 一次提交与结果状态 | true |
| `export` | 一次完整成片导出 | true |

## 2. schema 摘要

完整 JSON Schema 在
[`05-code/document_register.py`](./05-code/document_register.py)；这里记录跨模块会依赖的字段。

| 桶名 | 关键字段 |
|---|---|
| `project` | `width`, `height`, `fps`, `background`, `globalContext` |
| `asset` | `assetType`, `assetRef`, `processedRef`, `duration`, `width`, `height`, `cutoutStatus` |
| `clip` | `layer`, `visible`, `assetId`, `start`, `duration`, `sourceIn`, 几何字段, `videoRef` |
| `annotation` | `kind`, `label`, `layer`, `visible`, `bbox*`, `pointsJson`, `refImageAssetId`, `start`, `duration` |
| `guide_keyframe` | `parentId`, `time`, `bboxX`, `bboxY`, `bboxW`, `bboxH`, `easing` |
| `reference_frame` | `at`, `enabled`, `targetClipId`, `label` |
| `send_profile` | `provider`, `strategy`, `model`, `duration`, `ratio`, `paddingSec` |
| `send_job` | `status`, `provider`, `strategy`, `model`, `promptSnapshot`, `editedFramesJson`, `taskId` |
| `export` | `status`, `preset`, `fileRef`, `width`, `height`, `fps`, `duration` |

## 3. 兼容边界

- `annotation.kind` 可为 `box | text | image | arrow`；老项目缺少 `kind` 时按 `box` 处理。
- `guide_keyframe.time` 相对所属画面引导的 `start`；首尾之外保持最近位置，
  相邻位置之间线性变化。第一版只用于矩形和图片。
- `send_profile.mode` 暂时保留给老项目和审查快照，新的生成链路以
  `provider + strategy` 为准。
- 图片去背景失败只写 `cutoutStatus/cutoutError`，原图仍可继续使用。
- `reference_frame` 不内嵌进 `send_profile`，避免多次选择、删除和协作更新互相覆盖。
