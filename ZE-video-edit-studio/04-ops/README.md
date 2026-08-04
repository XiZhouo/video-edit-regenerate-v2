# 04-ops — 运维

skill 上线运行时的工程问题。当前含**配置管理**与**部署端口**两项。

## 子节点

1. [01-config-management/](./01-config-management/) — 配置管理（多环境 / 默认值 / 热更新）

## 部署端口（deploy.json）

skill 的 `deploy.json` 声明端口；权威校验/登记在 **ZE-skill-deploy**（`server-deploy/scripts/ports.py` + `ports.json`，见 `modules/port-governance.md`）。

- `port`：**对外**端口，落在 `8000–8999`，bind `0.0.0.0`。
- `internal_ports`（可选，默认 `[]`，**全兼容**）：进程内 **loopback sidecar** 端口数组，每项：
  ```
  { "port": 18000-18999, "name"?: str, "bind_host"?: "127.0.0.1", "proxy"?: <代理声明> }
  ```
  落在 `18000–18999`、默认 bind `127.0.0.1`（不对外）。约定**对外 8xxx ↔ 内部 1 8xxx**配对。

本 skill 当前无 sidecar、不声明 `internal_ports`；长出 sidecar（如本地推理服务）时再加，`ports.py` 已端到端支持、无需改部署侧代码。

## GPU 部署旋钮

GPU 显存预算、绑卡和 idle unload 的部署 env 不进设置面板；它们随端口登记写在 **ZE-skill-deploy** 的 `ports.json.gpu`。常驻 GPU 模型必须在 build 的 `02-mod/06-resources.md` 写资源/生命周期合同,上线时在 `ports.json.gpu.idle_unload` 登记默认卸载秒数（不超过 1800s）和实现文件；需要部署覆盖的 env 放 `gpu.env`,由 `ssh_deploy.sh` 写成远端 `.gpu-env`。
