# 状态声明纪律(Status Discipline)

> 随工具链层(`scripts/` + `_toolchain_contract.json`)同步到所有 ZE skill。人和 AI 改 skill 都照此办。

## 为什么有这条
完成状态(某能力建没建、某编辑器接没接)若**手写在散文里、又没门禁盯着**,代码一前进、文档没人改,
状态就过期 —— 文档自身就成了"乱、不全面"的来源。机器可读源(manifest / `__all__` / `_contract`)
天生不漂(有门禁守),散文里程碑表则高危。本纪律把"状态真相归机器源"钉成规矩。

## 四条规矩
1. **单一真相**:完成状态只在机器可读源声明 ——
   - 编辑器是否 adopted → `03-css-rules/EDITOR_MANIFEST.json`
   - AI 只读能力是否就绪 → `sdk/introspect.py` 的 `__all__`(每个 reader=一能力)
   - 各层接口版本 → 各 `_contract.json` / `02-mod/_locks/*.lock.json`
2. **散文不写会过期的状态**:SPEC / FUTURE_STATE 写**设计契约 + 指向真源**;里程碑表写"方向",
   不写"X 已/未做"这种会过期的断言。确需写状态时,**必须与机器源一致**(门禁会查)。
3. **改代码 = 同次提交同步真源**:动了某能力的实现状态,同一次提交改机器源(manifest / `__all__`)。
   **算子建好但 UI 未接,要显式标"已实现·未接",不许写成"即生效"**(本会话 layout SPEC 的教训)。
4. **提交 / 部署前跑门禁**:`scripts/verify_all.py` 含 `doc-status` + `ai-interface` 两道,红就不许过;
   部署 preflight 已跑 verify_all,所以每次部署自动强制。

## 守门的两道门禁(机器可锚定的部分)
- **`scripts/doc_status_check.py`** —— 硬失败:`EDITOR_MANIFEST` 标 adopted 的编辑器,若在任一 SPEC
  **表格行**里被冠以前瞻词(待补/未来/pending/⛏/🟡/null 占位)→ 红。另出**前瞻词复核清单**(报告,非致命),
  供人每次扫一眼"这条是真未来还是漂移"。
- **`scripts/ai_interface_check.py`** —— 硬失败:`introspect.__all__` 每个 reader 必须 `server.py`(路由)
  + `scripts/ze_command.py`(CLI 子命令)双暴露(API↔CLI 同形);§5 文档若把已建 reader 标前瞻词 → 红。
  已记录例外见该脚本顶部(目前仅 `settings_read`)。

## 诚实边界
门禁只覆盖**机器可锚定**的状态(editor adopted/pending、AI 能力 built/双暴露)。自由散文
("何时用某编辑器""某交互即生效")无法自动校验 —— 靠 `doc_status_check` 的报告段 surface +
本纪律 + 人复核兜底,**不假装全自动**。新增能力别图省事进门禁的"例外集",例外要带理由、要少。
