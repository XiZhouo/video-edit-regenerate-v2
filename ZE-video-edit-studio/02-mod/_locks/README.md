# _locks — 受管层独立复制替换的锁(mod 侧,sync 永不覆盖)

ZE build v3 的受管层各自可独立替换、开发、迭代、定制:

| 层 | 是什么 | 复制替换目标 | 契约 | lock |
|---|---|---|---|---|
| **core 代码** | 框架机制 | `01-core/` + `sdk/` + Surface 实际需要的 `server.py` / `webapp/src/` + `scripts/ze_command.py` | CODE 接口版本 + 受管复制清单纯内容摘要；破坏性由 Public Surface 门禁判定 | `core.lock.json` |
| **css** | 视觉令牌/模块 | `03-css-rules/` | CSS 契约(`--ze-*` + `.ze-*` + `data-*` 词表) | `css.lock.json` |
| **ops** | 运维/配置 | `04-ops/` | OPS 契约(配置 schema) | `ops.lock.json` |
| **i18n** | 框架词表 | `05-i18n/` | I18N 契约(框架 key 词表 + resolver API) | `i18n.lock.json` |
| **toolchain** | 同步器与门禁 | `scripts/verify_all.py` / `scripts/skill_sync.py` / `scripts/*_check.py` | Toolchain 契约(校验与同步行为) | `toolchain.lock.json` |
| **mod** | 业务策略(**作者唯一手写**) | 永不被复制替换 | — | — |

## 为什么 lock 放在 mod 侧
`Skill.Sync` 复制受管层时,**只碰 `ZE_LAYER_MANIFEST.json` 声明的受管目录 + 具名框架文件,永不碰 `02-mod/`、`.data/` 与 `product.dirs`**。lock 放 `02-mod/_locks/` 因此能跨复制存活,记录"这个 skill 当前贴着 build-v3 各层的哪个 `interface_version`"。

直接复制安全的完整路径所有权见 [../../DIRECT-COPY-FLYWHEEL.md](../../DIRECT-COPY-FLYWHEEL.md)。

## 怎么用(`scripts/skill_sync.py`,已实现)
```
python scripts/skill_sync.py --target <skill_dir>            # dry-run,只报告
python scripts/skill_sync.py --target <skill_dir> --apply    # 真复制 + bump lock
python scripts/skill_sync.py --target <skill_dir> --layers css   # 只某层
python scripts/skill_sync.py --init-locks                    # 给 build-v3 自身写 lock 基线
```
对每层比对源契约(版本源 `sdk/_contract.py` / `03-css-rules/_contract.json` / `04-ops/_contract.json` / `05-i18n/_contract.json` / `scripts/_toolchain_contract.json`)
与本 lock 的 `interface_version` + checksum →
- 相等 → `same`(跳过)
- MAJOR 同、checksum 变 → `compat_bump`(整树复制 + 自动 bump lock)
- MAJOR 升 → `incompat`(**拒绝复制该层** + 出 hint;按对应 `MIGRATIONS.md` 改 02-mod 后手动 bump)
- 缺 lock → `initialize`(复制 + 建 lock)

incompat 时 `exit 1`(可做 CI 门禁)。复制目标只含受管路径,**永不碰** `02-mod/`(业务 + `_locks`)与 `.data/`(运行态)。过渡期 `ZE_SURFACE.json.web.product_dir` 指向的 `webapp/src/product` 属产品层,由 toolchain 1.24+ 保留并从 core checksum 排除；toolchain 1.25+ 会把该边界写入 `ZE_LAYER_MANIFEST.json` 并由 `managed_drift_check.py` 校验。

core checksum 必须由与复制器共用的文件 inventory 生成：会复制的 regular file 必须进入摘要，不会复制的 `product_dir`、gallery、构建产物与系统垃圾不得进入。Web、API、CLI 三种 Surface 各按实际投影计算，不能给 headless 目标写一份包含未复制 Web/server 文件的全量锁。

每个 `*.lock.json` 都是严格的 `ze.interface_lock.v2` 权威文件，而不是“尽量读取”的缓存：必须是普通文件，字段完整且无重复/未知键，`layer` 必须与文件名一致，checksum 必须是 64 位小写十六进制。Skill.Sync、Fleet 和 drift gate 共用同一解析合同；即使最后一个重复键碰巧等于当前值，也必须 hard-red，不能判 `same`。

drift gate 始终由当前可信工具链执行：目标 skill 自带的旧 verifier 不拥有裁决权，`--upstream` 也不会关闭目标形状与 lock 一致性检查。

## 红线
- 运行态只放 `.data/`(复制目标之外)。放进 01-core/03-css-rules/04-ops/sdk 的任何状态会在每次迭代被 rmtree。
- 外观定制走 mod/theme overlay;历史 `03-css-rules/01-tokens/tokens.css` 末尾 `html[data-mod]` 覆盖块是过渡方案,定制后该 skill 不再 `--layers css` 同步(整层复制会覆盖)。
- Surface 退役不是“改一行配置”：从 Web/API 降为 CLI 前，必须先迁移产品数据并移除旧 `webapp/src` / `server.py`。否则旧受管文件会被投影排除而形成假绿，所以同步与校验会在写 lock 前拒绝。
- 业务布局/默认字段只在 `02-mod/04-layout.config.ts`(不在框架 App.tsx)。
- 下游受管层有本地 diff 时,先按 DIRECT-COPY-FLYWHEEL 分类:回灌 build-v3 / 迁到 `02-mod` / 删除临时改动。
