# 视频编辑再生成的 Document 定义

本工具只保存一条完整时间线。原视频和每次生成结果都是独立 `clip` 图层；画面引导
只参与预览与提交，不会进入最终导出，也不会删除范围外片段。

内部实体名 `annotation` 为兼容 V2 既有项目而保留，产品界面统一称为「画面引导」。

## 1. entity_type 清单

| entity_type | base | 一句话说明 |
|---|---|---|
| `project` | 手写 | 画布、帧率和全局上下文 |
| `asset` | 手写 | 原视频、用户图片及其透明处理结果 |
| `clip` | 手写 | 完整时间线上的视频图层 |
| `annotation` | 手写 | 带出现时段的框选、文字、素材或路径 |
| `guide_keyframe` | 手写 | 矩形或图片在某个关键画面的当前位置与大小 |
| `reference_frame` | 手写 | 截图式提交中由用户明确选择的参考画面 |
| `send_profile` | 手写 | 模型、提供方与提交方式 |
| `send_job` | 手写 | 一次生成任务的不可变提交快照 |
| `export` | 手写 | 完整成片的导出记录 |

这些形状包含视频几何、时间区间、文件引用和生成快照，类型库没有可直接复用且不
丢语义的原型，因此采用逃生舱 schema；完整字段以
[`05-code/document_register.py`](./05-code/document_register.py) 为准。

## 2. 核心关系与不变量

```text
project
├── asset(video/image)
├── clip ──> asset
├── annotation ──> clip / asset(image)
├── guide_keyframe ──> annotation
├── reference_frame ──> clip
├── send_profile
├── send_job
└── export
```

- 原视频始终保留；AI 结果新增为更高一层 `clip`，不原地覆盖。
- 每个画面引导都有自己的 `start + duration`，可独立控制出现时间。
- 图片素材也是画面引导，因此享有独立时间段和画面位置。
- `reference_frame` 是显式选择的修改画面，数量为 1 到 N，不等同于首尾帧。
- `annotated_video` 提交带引导的视频；`modified_frames` 提交干净视频与 1 到 N 张修改画面。
- 生成范围外的原片不删除；最终导出按完整可见图层合成。

## 3. 协作与保存

```yaml
collaboration_mode: with_save
save_policy: auto_commit
```

所有业务实体都是独立协作单元，字段修改通过 Command / Operation 进入版本与撤销链。

## 4. demo 项目

demo 保留一段原片、一个框选引导和一个生成结果图层，用于演示：

- 引导只影响生成，不进入导出；
- 生成结果是新图层；
- 关闭生成层即可回看完整原片。
