# 变更传播安全网（Change-Propagation Safety Net）

build-v3 是 159 个 v3 skill 的上游框架。它的**公共表面**（py `sdk.__all__` / fe 各 barrel 的具名导出）
是下游 02-mod 的依赖契约。删/改一个公共符号 = 对下游的 breaking change。本文件是「改公共表面时怎么做」的
唯一程序说明；各机制的实现细节在对应脚本 docstring，本文件不重复，只串流程。

## 为什么需要它（两次实证事故）

`skill_sync` 按**层校验和**判 same/compat_bump/incompat，**看不见符号级删除**：

- `sdk` 删了 `llm_vision_schema_call`，audit 的 `02-mod` 还 import 它 → lock 判 compat_bump、同步成功、
  typecheck 全绿，**python 起服才 ImportError**。
- `HeaderZone` 漏出 editor barrel → tsc 路径映射能过，**vite/rollup build 才报** not exported。

共性：`INTERFACE_VERSION` 手填、"删 = MAJOR" 全靠纪律，忘了就漏；typecheck ≠ build ≠ runtime。
安全网把这三件事从"靠记得"变成"机器拦"。

## 三道机制

| 机制 | 脚本 | 在哪跑 | 拦什么 |
|---|---|---|---|
| **P1 表面 diff** | `scripts/surface_diff_check.py` | `verify_all` 的 `surface-diff` 门禁（每次 build-v3 自检；下游软跳过） | 删/改名公共符号 → RED，逼出 MAJOR + 迁移纪律 |
| **P2 消费扫描** | `scripts/consumer_scan.py` ＝ `fleet_health_check.py --impact` | 按需 / 发布前（扫 159 个下游 02-mod） | 给定删除集 → 逐 skill 的 `file:line` 破坏点 |
| **P0 真验证** | `scripts/sync_smoke.py` | 同步后 / 部署前（对单个 skill） | py `from sdk import` 真可解析 + `npm run build` 真过 |

`PUBLIC_SURFACE.lock.json`（本目录）= P1 的基线快照，记录当前对外的 py + fe 符号集。

## 程序：我要改 build-v3 的公共表面

1. **只是新增符号** → 放行。`surface-diff` 只拦删/改名；加导出是兼容增量。改完跑 `surface_diff_check.py --update`
   把新符号纳入 lock（diff 进 commit 留痕）。

2. **改了某符号签名（py）** → `surface-diff` 出 `[warn]`（收窄可能破坏、加可选参不破坏，二义，自己判）。
   破坏性收窄按删除处理（见 3）；纯增量 `--update` 重打 lock。

3. **删 / 改名一个公共符号** → `surface-diff` RED，verify_all 过不了。两条路：
   - **误删**（比如漏写 barrel 导出）→ 补回导出，RED 自动消失。
   - **有意移除** → 按门禁提示依次做：
     1. 跑 `python3 scripts/consumer_scan.py --py <删的py> --fe <删的fe>`（surface-diff 的 RED 输出里
        已给出这条可复制命令）→ 得到**全舰队需要变更的位置**（逐 skill `file:line`）。
     2. 对每个受影响 skill 的 `02-mod` 做迁移（import 点 + 调用点都在 P2 报告里），或提供 shim 过渡。
     3. 在对应层写 `MIGRATIONS.md` 说明迁移方式 + bump 该层 `INTERFACE_VERSION` 的 MAJOR。
     4. `python3 scripts/surface_diff_check.py --update` 重打 lock（删除落进 lock 的 git diff，可审计）。

4. **同步 / 部署前** → 对目标 skill 跑 `python3 scripts/sync_smoke.py <skill_dir>`，真 import + 真 build。
   这是最后一道：即便前面漏了，起服前也能挡住（两次事故都是这一关本该拦下的）。

## 设计边界

- P1 是**上游创作门禁**：表面由 build-v3 定义、下游只消费，故 `surface_diff_check.py` 不进受管 toolchain、
  不下发；`verify_all` 用 `_run_if_present`，下游缺脚本即软跳过（同 css_layer_guard 等皮肤门禁）。
- 当前覆盖 **py 符号 + fe barrel 具名导出**。i18n key / css 类 / ops knob 的删除尚未纳入表面（未发生事故，
  留作后续）。`export * as NS` 算单个具名导出（可追踪）；裸 `export * from` 枚举不到、会出 `[warn]`。
- 这套网**防 breaking 传播**，不替代各层自己的契约门禁（i18n parity / css contract / type registry 等）。
