# I18N 层迁移记录(框架侧,随 05-i18n 整树复制)

I18N 契约见 `02-mod/_locks/i18n.lock.json`:= 字典 key 词表 + `t()/getT/translate` API + `setModMessages` 注入点。

## 版本规则
同 core/css/ops:**MAJOR = 破坏契约**(删/改任一 key,下游 `t('ns.key')` 引用断)→ `Skill.Sync` 报 incompat;**MINOR/PATCH = 加性**(新增 key / 改译文)→ compat_bump(重拷字典、自动 bump lock)。译文改动也会变 checksum(文件内容 sha256),判 compat_bump = 重拷,非破坏。四层(core/css/ops/i18n)各自独立 `interface_version`,可独立复制替换。

## 记录
### 3.33.0 (2026-07-19) — AI 行为具象化文案(加性)
- guide 命名空间 +12 键(zh+en 镜像):回执 `aiEditedOne/aiEditedMany/taskStart/taskDone/taskError`;
  确认拉起 `pendingMeta/pendingAsk/pendingNeed/approve/later/approveFailed`。纯新增 key → MINOR。

### 3.30.0 (2026-07-10) — 项目拓扑意图与响应证据文案（加性 MINOR）
- 新增 `auth.projectMutation.*` 27 个 zh/en 镜像 key，覆盖响应解码/契约不匹配、项目与示例列表无效、匿名示例身份缺失、创建/复制/删除/重置结果未知、generation 过期、列表快照保留，以及意图暂存、证据丢失、人工修复和持续对账提示。
- `authApi` 与 `projectMutationIntent` 的安全错误统一经过 `getT()`；未知提交结果不再依赖裸中文或不可翻译的底层异常。纯新增 key，`3.29.0 → 3.30.0`。

### 3.29.0 (2026-07-10) — 独立保存状态文案（加性 MINOR）
- 新增 `persistence.*`：不可用、已保存、保存中、尚未保存、保存失败、状态待修复与 tooltip 前缀；zh/en 镜像。
- 用于 Footer 的 `PersistenceState`，不复用 `sync.*`，避免把“协作事件已追上”误译成“业务数据已耐久”。纯新增 key，`3.28.0 → 3.29.0`。

### 3.0.0 (2026-06-13) — 删 layout.* keys(破坏性,配合 core 1.33.0 移除运行时分屏/关闭)
- 删 `layout.splitPane`/`layout.closePane`(该 namespace 仅此两键,整块删除)。唯一消费者 `LayoutRenderer` slot chrome 同批移除;grep 下游(design/finalizer 02-mod+webapp)零 `t('layout.*')` 引用。**删 key 按规则 MAJOR → 3.0.0**。

### 2.6.0 (2026-06-12) — header-v2 keys(加性;补记,bump 当日漏记层内条目)
- 配合 core 1.31.0:新增 `file.duplicate/listEmpty/justNow`、`header.fileSwitcherAria`、`share.members/ownerBadge` 等(FileSwitcher/AclPanel)。纯加 key → compat_bump **2.5.0→2.6.0**。

### 2.5.0 (2026-06-11) — 属性面板/管线操作台 keys(加性)
- 配合 core 1.28.0:新增 `editor.nSelected`(多选徽记「已选 {n} 项」)、`editor.mixedValues`(聚合编辑混合态 tooltip)、`editor.pipelineStartConsole/pipelineRun/pipelineRunning`(StartConsole)、`editor.pipelineRuntimeMissing`(requires_runtime 缺位标题)、`editor.pipelineInterruptedHint`(中断续跑提示)、`editor.pipelineExportJson/pipelineExportMd`(notebook 导出);`task.status` 补 `skipped/interrupted/stale`(RunPhase 显示词全量过 RUN_STATUS_ALIAS 收口点后不再裸词)。纯加 key → compat_bump **2.4.0→2.5.0**。

### 2.4.0 (2026-06-11) — 卡片列表编辑器 thumbSize key(加性)
- 配合 core 1.27.0 卡片列表抽象:新增 `editor.thumbSize`(zh「缩略图尺寸」/ en「Thumbnail size」,工具条档位循环钮 title)。eye/lock/search/filterAll 复用既有键(editor.hide/show/lock/unlock/search/filterAll)。纯加 key → compat_bump **2.3.0→2.4.0**。

### 2.3.0 (2026-06-10) — pipeline notebook editor keys(加性)
- 配合 core pipeline editor:新增 `editor.pipeline*` 系列键(zh/en),覆盖固定管线标题、step cell、输入/输出、日志、interrupt/continue/retry、step rerun/run-from-step 等 notebook 控制文案。纯加 key → compat_bump **2.2.0→2.3.0**。

### 2.2.0 (2026-06-10) — editor.notePlaceholder key(加性)
- 配合 css 15.6.0 placeholder 法(禁与 label 同文):ReviewEditor 批注 placeholder 从复用 `editor.note` 改为新键 `editor.notePlaceholder`(zh「写下审阅意见…」/ en「Add review comments…」)。纯加 key → compat_bump **2.1.0→2.2.0**。

### 2.1.0 (2026-06-10) — slot.rendererMissing key(加性)
- 配合 core 1.20.0 通用注入式 custom 面板:新增 `slot.rendererMissing`(zh/en),当 slot `editor:'custom'` 的 `rendererKey` 在 `@mod/05-renderers` 找不到时提示。纯加 key → compat_bump **2.0.0→2.1.0**。

### 2.0.0 (2026-06-03) — 语言可插拔 + 业务层 N 语言化
- **新增 `locales.ts` 语言注册表**(`LOCALE_CODES` / `LOCALE_META` / `DEFAULT_LOCALE`):增减语言的**唯一真源**。`Locale` 类型、`DICTS`、`detect`、`<html lang>`、设置菜单语言项全部从它派生。加日语 = 改 `locales.ts` + 建 `ja.ts`(`as Dict`)+ bump 本层版本 → Skill.Sync 全舰队下发框架 ja。
- **业务层 N 语言化(破坏性)**:`02-mod/09-i18n.ts` 由 `export const zh/en` 改为 `export const messages: Partial<Record<Locale, …>>`(locale-map);`setModMessages(modZh, modEn?)` 改为 `setModMessages(byLocale)`,遍历 `LOCALE_CODES` 注入、缺译回落 `DEFAULT_LOCALE`。**旧 `(modZh, modEn)` 签名失效 → MAJOR**(v3 无下游,零爆炸半径)。
- **补完框架迁移**:footer / editor(8 类)/ primitives / slotAdapters / authApi 全量走 `t()`,新增 `footer`/`editor`/`tool`/`slot` namespace + auth 两个错误 key。
- **门禁反转**:`i18n_contract_check.py` 从 opt-in 白名单改为**默认全扫**(`SCAN_ROOTS` 下 .ts/.tsx)+ 收缩式 `LEGACY_EXEMPT`(已清空);新写组件出现裸中文即红。mod seed 数据(`02-mod/04-layout.config`)按「Document≠UI」豁免。
- **配套工具**(随工具链同步层下发):`i18n_dump_keys.mjs`(esbuild 取真词表 key)、`i18n_sync.py`(`--check` 报漏配 / `--fill` 自动补占位到每本词表)、`i18n_audit.py`(missing-key / parity / registry,并入 `verify_all`)。
- 标准工作流:写 `t('ns.key')` → `python scripts/i18n_sync.py --fill` → 填译文。

### 1.0.0 (2026-06-02) — 基线(i18n 独立成层)
- i18n 从 core 内联硬编码中文,提为独立顶层 `05-i18n`(03-css-rules"看" / 04-ops"配" 的孪生:"说")。
- 纯 TS 词表 + resolver(无 React):`zh.ts`(`as const` 真源)、`en.ts`(`as Dict` 强镜像,缺 key 编译失败)、`index.ts`(`t/getT/translate` + `Dict`/`I18nKey`/`setModMessages`)、`locale-store.ts`(模块级观察者 + localStorage `ze.locale`)、`detect.ts`。
- React glue(`useT` / `Trans` / `PreferencesProvider` 应用 `<html lang/data-theme>`)在 core `04-user-facing/05-preferences/`;业务字典在 `02-mod/09-i18n.ts`(`mod.*`,App 经 `setModMessages` 注入)。
- 基线 key:`common.*`(save/cancel/close/confirm/loading)。标杆 surface(auth/settings/share/config)key 随组件迁移补全;门禁 `scripts/i18n_contract_check.py` 锁"已迁移文件零裸中文"。
