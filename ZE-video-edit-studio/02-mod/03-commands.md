# Command Registry

所有会修改项目的 Web / CLI / AI 操作都 dispatch 注册命令，不在 renderer 或 stage
里直接改 Document。

## 1. 业务命令

| id | 用户含义 | kind | touches | 主要结果 |
|---|---|---|---|---|
| `annotation.add` | 添加框选、文字、素材或路径 | native | `annotation` | 新建一个带独立时段的画面引导 |
| `annotation.remove` | 删除画面引导 | native | `annotation` | 软删指定引导 |
| `guide.keyframe.set` | 记录/更新当前位置 | native | `guide_keyframe` | 在当前画面记录矩形或图片的位置与大小 |
| `guide.keyframe.remove` | 删除当前位置 | native | `guide_keyframe` | 删除指定的位置记录 |
| `reference_frame.add` | 将当前画面加入参考 | native | `reference_frame` | 记录一个显式参考时刻；近似重复时 no-op |
| `reference_frame.remove` | 移除参考画面 | native | `reference_frame` | 删除指定参考时刻 |
| `asset.import` | 导入视频或图片 | native | `asset`, `project` | 登记素材；首个视频同步确定画布 |
| `asset.cutout` | 图片自动去背景 | stage | `asset` | 写入透明 `processedRef`，失败保留原图 |
| `send.compile` | 编译审查快照 | native | `send_job` | 兼容旧流程，只编译不派发 |
| `edit.send` | 生成修改结果 | stage | `send_job`, `clip` | 组合画面引导并提交，结果新增为视频图层 |
| `export.render` | 导出完整成片 | stage | `export` | 合成完整可见时间线，不包含画面引导 |

框架内建的 entity / document / pipeline 命令由 `register_builtin_commands` 注册，不在此重复。

`annotation.add` 的框选和路径支持黑、白、红三色，颜色随引导保存，不因删除后的编号
重排而改变。路径在画布上用按下—拖动—释放完成：按下确定箭尾，按住拖动时预览方向，
松开确定箭头；普通单击不新建，点中已有路径会选中它。工具保持激活，可继续添加下一条。
框选或路径落定后仍可选中，并在画布顶部、与绘制时相同的位置重新选择黑、白、红三色。

## 2. `edit.send` 的提交矩阵

| 提供方 | `annotated_video` | `modified_frames` |
|---|---:|---:|
| Omni | 支持 | 支持 |
| Seedance | 支持 | 支持 |

- 产品端不根据本地能力矩阵禁用组合；真实可用性以服务调用结果为准。
- `annotated_video`：发送带画面引导的视频；每个引导仅在自己的时间段出现。
- `modified_frames`：文件顺序固定为干净原视频、修改画面 1..N；每个生成区间至少有一张参考画面。
- 两种方式都不改变原视频实体；生成结果新增为高层 `clip`。

## 3. 慢任务边界

- `asset.cutout` 使用 `cpu.image`。
- `edit.send` 先使用 `cpu.ffmpeg` 制作提交文件，再使用 `api.videogen`。
- `export.render` 使用 `cpu.ffmpeg`。
- 慢任务失败以结构化状态回写，不伪装成成功。

实现入口：[`05-code/commands_register.py`](./05-code/commands_register.py)。
