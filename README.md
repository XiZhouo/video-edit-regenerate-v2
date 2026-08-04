# 视频编辑再生成 · 本地 V2

本目录来自 `ZE-video-edit-studio-交接包-20260727.tar.gz`，作为现有本地工具的
并行 V2 体验版保留，不覆盖根目录下的 V1。

- V1：`http://127.0.0.1:8787`
- V2：`http://127.0.0.1:8788`
- V2 应用源码：`ZE-video-edit-studio/`
- 共享视频领域源码：`ZE-video-timeline-domain/`
- V2 运行数据：`ZE-video-edit-studio/.data-v2-local/`（不会提交到 Git）

启动：

```bash
bash run-v2.sh
```

V2 当前保持交接包原有的“图层式视频精修台”逻辑，用于体验框架、交互和 UI。
现有工具的 Seedance 引导再生成、图片抠图、起终点关键画面、修改范围替换与原音频
回贴尚未移植，不能把 V2 当前的 AI 发送路径当成 V1 的等价替代。
