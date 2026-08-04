# 受管层纪律：video-timeline-domain

本仓是 ZE 视频四件套的**共享受管域层**唯一源仓。它以「build-v3 受管层同款模式」被
`sync_domain.py` 分发进三个消费 skill 的 `02-mod/05-code/_domain/`。

## 谁是源、谁是投影

- **源（唯一真值）**：`ZE-video-timeline-domain/src/ze_video_domain/`
- **投影（只读副本）**：
  - `ZE-video-edit-studio/02-mod/05-code/_domain/`（①，本轮落地）
  - `ZE-video-generation-timeline/02-mod/05-code/_domain/`（②，P0 挂载，待 schema 评审门）
  - `ZE-video-timeline-editor/02-mod/05-code/_domain/`（③，P0 挂载，待 schema 评审门）

消费 skill 的 mod 政策层只 `from _domain import …`，**绝不改 `_domain/` 内部**。

## 三条硬纪律（与既有 ZE 纪律同源）

1. **必回流上游**：任何在工具内对 `_domain` 的改动，必须先回到本仓改源、再 `--sync` 下发。
   `sync_domain.py --verify <skill>` 检出 `DRIFT` 即代表有人私改了投影——处置是回流，不是覆盖重同步。
2. **sync 前必跑消费方门禁**：波次同步前，对每个消费 skill 跑 `verify_all`，确认不 clobber 下游价值
   （sync-never-clobber-downstream-value 硬令）。
3. **版本锁登记**：每次 `--sync` 在投影里写 `_DOMAIN_LOCK.json`（version+checksum+synced_at），
   并在本仓 `domain_lock.json` 登记消费方。`--verify` 据此判 `OK / BEHIND / DRIFT / MISSING`。

## 为什么不是「单 skill 三视图」

三工具已分别在线部署（8111 等）、用户群与迭代节奏不同，合并成巨型 skill 违背 ZE 小工具哲学、
回滚半径大。统一文档 `ze.video_project.v1` + 共享域层已拿到「综合设计」的全部好处：
同一项目三处可开、引擎不三写。

## 与 build-v3 受管层的关系（需反哺）

本仓复用了 build-v3 受管层的**形态**（源→投影、版本锁、drift 检查、回流纪律），但它是一个
**mod 级、跨兄弟 skill** 的共享层，而 build-v3 现有受管层都是 **framework→mod** 的纵向层。
build-v3 需要把「mod 级跨 skill 共享域层」正式纳入受管层体系（sync SOP + `_domain` lock gate +
`managed_drift_check` 覆盖 `_domain/`）。详见随附的《反哺标准 skill 说明》HTML。
