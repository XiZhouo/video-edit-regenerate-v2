# ZE-video-timeline-domain

共享受管域层，为 ZE 视频四件套（① edit-studio / ② gen-timeline / ③ timeline-editor /
④ batch-foundry）提供**统一实体 schema + 共享引擎 + 编译层 + 策略层 + 持久化**。

框架无关（不 import build-v3 sdk），除显式 ffmpeg / videogen 接缝外无网络/IO，因此可完整单测、可搬移。

## 内容（`src/ze_video_domain/`）

| 模块 | 职责 | 管线段 |
|---|---|---|
| `schema.py` | `ze.video_project.v1` 九实体不可变工厂 + 规范化文档容器 | V1/统一文档 |
| `clip_math.py` | 纯时间线数学（轨道/标段/区间并集/padding/span 外沿锚/文件名守卫） | V3 |
| `ffmpeg_ops.py` | 本地 ffmpeg 纯 argv 构造器 + 执行器（截段/抽帧/拼合/**mask_merge**） | V5/V8 |
| `send_compiler.py` | 模式阶梯 × 能力路由 × 命名指代→模型语法 × 引用 lint × 安全改写 × 提示词模板 | V4+V6 |
| `capability_matrix.py` | 11 模型能力 + 引用语法表（`@file_N`/`@ImageN`/`[Image N]`/`<<<image_N>>>`/自然语言） | V4 |
| `safety_rewrite.py` | 真人/名人名 → 外观描述（结构化 diff，绝不静默通过） | V4 |
| `candidate_flow.py` | 候选采纳 / stale / 空段自动采纳（纯逻辑） | V8 |
| `verify_report.py` | 机检红黄绿汇总（黄不拦，红=时长/帧率异常禁采纳） | V8 |
| `strategy/` | 可插拔 JSON 策略（general-v1 / ad-short）+ provider 链 codex-skill→gateway→skip | V6 |
| `persistence/` | 原子写 `.tmp→replace` + 上游任务重启恢复模型 | 闭环持久化 |
| `videogen_client.py` | 视频服务瘦客户端（**遇传输错误抛异常**，requires_runtime 分类交 stage 层） | V7 |

## 用法

```bash
# 单测（57 用例）
python3 -m pytest tests/ -q

# 下发到消费 skill 的受管 _domain 层
python3 sync_domain.py --sync   /path/to/ZE-video-edit-studio
python3 sync_domain.py --verify /path/to/ZE-video-edit-studio   # OK / BEHIND / DRIFT / MISSING
python3 sync_domain.py --list                                   # 已登记消费方
```

受管层纪律见 `_MANAGED_LAYER.md`。需反哺标准 skill 的细节见随规划包的《反哺标准 skill 说明》HTML。

## 状态

- **P0 已落地**：schema v1 定稿 + 全部引擎 + 编译层 + 策略层 + 持久化 + sync 工具 + 57 单测绿。
- **待 schema 评审门**：把 `_domain` 挂进 ②③ 并证明「行为零变化」（既有 e2e 原样过）——
  该步按规划有人工评审门，未在本轮执行。
- ① `ZE-video-edit-studio` 已作为首个消费方同步并接线（见该 skill）。
