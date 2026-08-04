# 本地软件更新

本能力把 System 的只读 `ze.update-manifest.v1` / `ze.update-offer.v1` 交接为设备本地的软件安装交易。实现位于 `sdk/local_update_agent.py`，由受信的 Workspace 或 Launcher 进程调用，不注册普通 Web API。

## 权限边界

- Agent 的唯一写根由 Launcher 创建，例如应用数据目录下的 `updates/`。API 不接收项目目录，状态、Journal 和 Receipt 不记录项目路径。
- Manifest 内的公钥信息不能授予自身信任。`TrustedReleaseKey` 必须来自安装包预置或独立受管的信任根，并绑定 Series、Organization 和 Artifact Role。
- 浏览器传来的“可以更新”不能直接触发安装。Launcher 必须提供 System 解析出的精确 Offer，以及刚刚观察到的安全点与用户选择。
- Workspace 平台安装包交给操作系统/Tauri 安装器；Server API 交给 Deploy；Project Schema 使用独立的预检、备份、确认和迁移交易。

## 本地目录

```text
updates/
├── state.json
├── activation.json                 # 仅交易进行中存在
├── staging/<component>/<version>/
│   ├── artifact.bin
│   └── verified.json
├── components/<component>/
│   ├── current.json                # 原子指针，不是项目文件
│   └── versions/<version>/
│       ├── .ze-install.json
│       └── payload/
└── receipts/update-*.json
```

版本目录不会覆盖作者源码。候选包先核对精确大小、SHA-256、Artifact detached signature、Manifest signature、信任范围、有效期和单调版本，再进入 staging。ZIP/TAR 解包拒绝路径穿越、链接、特殊文件、重复路径和异常解压膨胀。

## 激活和恢复

1. Launcher 重新确认没有未保存内容或活动任务，并根据用户选择创建一次 `InstallAuthorization`。
2. Agent 验证 staging 仍与同一 Manifest 对应，把候选内容放进独立版本目录。
3. Agent 先写 activation journal，再原子替换 `current.json`，随后执行 Launcher 提供的健康检查。
4. 健康检查通过后把版本标记为 `healthy`；失败则恢复旧指针。若进程在中途终止，新 Agent 构造时读取 Journal 并完成回切或确认已经提交的健康版本。

最小调用面：

```python
from sdk import (
    InstallAuthorization,
    LocalUpdateAgent,
    TrustedReleaseKey,
)

agent = LocalUpdateAgent(update_root, trusted_keys)
agent.stage_file(manifest, offer, downloaded_artifact, detached_signature)
receipt = agent.activate(
    manifest,
    refreshed_offer,
    InstallAuthorization(
        reason="user-confirmed",
        safe_point_confirmed=True,
        user_confirmed=True,
    ),
    health_check=lambda payload: launcher_probe(payload),
)
runtime_path = agent.resolve_current("dialogue-runtime")
```

`health_check` 是受信 Launcher 的本地回调，不从 Manifest 读取命令，避免发布元数据变成任意命令执行入口。
