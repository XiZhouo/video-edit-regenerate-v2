# 重资源合同

注册实现见 [`05-code/resource_register.py`](./05-code/resource_register.py)。三个闸门均为
Repository 级单例，并由 `StageSpec.resource_scopes` 统一获取。

## 1. 资源清单

| resource | kind | scope | 预算 | enforcement_scope | rate_per_sec | on_violation | 验收 |
|---|---|---|---|---|---|---|---|
| 视频生成服务 | 槽位+吞吐 | `api.videogen` | max=2 | process | 1 次/s | wait | 在途生成请求 ≤ 2 |
| ffmpeg 重编码 | 槽位 | `cpu.ffmpeg` | max=2 | process | — | wait | 同进程 ffmpeg stage ≤ 2 |
| 图片去背景 | 槽位 | `cpu.image` | max=1 | process | — | wait | 同进程处理任务 ≤ 1 |

## 2. 调用路径

| scope | path | path_count | per_path | sub | demand |
|---|---|---:|---:|---:|---:|
| `api.videogen` | `pipeline:edit.send` | 1 | 1 | 1 | 1 |
| `cpu.ffmpeg` | `pipeline:edit.send` | 1 | 1 | 1 | 1 |
| `cpu.ffmpeg` | `pipeline:export.render` | 1 | 1 | 1 | 1 |
| `cpu.image` | `pipeline:asset.cutout` | 1 | 1 | 1 | 1 |

单个 stage 内不做并发 fan-out；跨项目并发由同一个 GateRegistry 限制。

## 3. 生命周期

本工具不在 server 进程常驻 GPU 模型。图片去背景库按任务调用；ffmpeg 为一次性子进程；
视频生成由外部 API 完成，因此不需要 GPU idle-unload watcher。
