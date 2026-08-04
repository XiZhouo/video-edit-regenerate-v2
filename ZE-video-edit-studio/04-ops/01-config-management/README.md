# 01-config-management — 配置管理

> 一句话：多环境配置 + 默认值 + 热更新。

dev / staging / prod 三环境配置 / 默认值层级 / 热更新协议 / 配置变更审计。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — 完整规则 + 技术文档 (待用户带着填)
- `config.py` / `config_schema.py` / `registry.py`(实现;入口经 `sdk` 门面)

## 怎么用

mod 作者消费口径(机制/种类/作用域见 [SPEC.md](./SPEC.md)):

1. **声明配置项**:大白话写进 `02-mod/08-config.md`,代码落 `02-mod/05-code/config_register.py`(文件缺省即跳过):

```python
from sdk import ConfigRegistry

def register(cfg_reg: ConfigRegistry) -> None:
    # 本 skill 特有旋钮(自动出现在 header ⚙ 设置面板"工具配置"区)
    cfg_reg.declare("demo.shot_style", "text", default="",
                    label="出图风格前缀", scope="tool", group="本工具")
    # 覆盖框架通用组默认:同 key 再 declare 即覆盖(只改默认,不改 kind/choices)
```

   框架通用组(`ze.llm.*` 路线 / `ze.image.*` 图片服务)由 `seed_framework_config` 预装,mod 白拿不用声明。

2. **取值**:业务代码 `repo.config.get("ze.image.model")`(mod stage 里经 `getattr(registry, "config", None)`)。生效优先级:`.data/config.json` 面板改值 > env(声明了 `env_var` 的项)> 默认源 > 声明默认。
3. **env 覆盖**:框架运行旋钮统一 `ZE_*` 前缀、启动时从环境变量读,设置面板只读展示(命名约定见 SPEC.md §4)。
4. **secret 脱敏**:密钥项 `cfg_reg.declare(..., "secret", secret=True, default_source="hupo:API_KEY")`——面板经 `snapshot()` 回显 `***`、绝不回明文;业务代码 `get()` 取明文供实际调用;默认源是 `ZE-hupo-api/.env`。
5. **原子更新**:服务端 PUT 只调用 `cfg_reg.update_values(config_path, updates, expected_version=...)`。它在一个进程锁内先构造、校验并完整序列化 candidate，再经唯一临时文件 `write → flush → fsync → os.replace → 父目录 fsync` 落盘，最后才切换内存；不要把兼容的内存方法 `set_values()` 与 `save_values()` 拼成新写路径。

## 失败时会发生什么

- 临时文件创建、写入、flush、文件 fsync 或 replace 失败：抛 `config_write_failed`，内存和磁盘都还是旧值，临时文件会清理，可安全重试。
- replace 已成功、父目录 fsync 却报真实 I/O 错误：文件已经是完整新值，内存也跟随新值，然后抛 `config_durability_unknown`。此时不能盲重试；错误的含义是“掉电耐久性未知”，不是“保存未发生”。平台明确不支持目录 fsync 时会 best-effort 成功，并可由 `directory_fsync_supported` 观察。
- `/api/config` 会返回当前 `version`，设置面板 PUT 时原样带回 `expected_version`。陈旧页面固定得到 `config_version_conflict`；`config_durability_unknown` 对应的幂等键会被冻结成 outcome-unknown 修复凭据，不会自动执行第二次。
- 该 PUT 必须携带 `expected_version`，且只允许 super-admin；普通项目 edit/owner 不等于全局运维权限。
- 已声明 key 禁止再 `declare`；mod 只改默认时调 `override_default`，不能改掉 secret/readonly/endpoint allowlist。
- `config.json` 不存在：按空 override 启动；文件存在但不可读、JSON 损坏、顶层不是 object 或包含非法 override：抛 `config_load_invalid`，保留原内存，绝不静默退回默认值。

当前 `expected_version` 与线程锁只承诺**单进程**内不丢更新。真正多进程 CAS、数据库事务、跨设备同步和耐久审计属于 W06 的事务存储层。
