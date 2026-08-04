# CSS 清理台账 · Cleanup Ledger(单一追踪点)

> 把散在各 `code.css` 的 `⊘deprecated`/`⊘defer`/`deprecated-dead` in-file 标记,以及废令牌、旧版本处置,**收敛到这一处**。反熵:一个东西的"该不该清、何时清、为什么留"只在这张表里追踪,不散在注释里。
>
> 家规:**删 `.ze-*` class / `--ze-*` token = MAJOR bump**(下游 Skill.Sync 报 incompat)。故清理批量攒到一次 MAJOR 做,不零敲碎打。

## 1. 已删(css 18.0.0,2026-07-01)

批量清理 v1。四个 class 全仓 + 4 下游(packer/foundry/audit-v3/playable-v3)的 `.tsx` 消费均 =0 核实后删;README「对外表面」同步去名。

| class | 原址 | 死因 | 核实 |
|---|---|---|---|
| `.ze-header__menu-submenu` | `02-atoms/shell/header-shell` | FileMenu 退役遗留(最近项目子菜单) | tsx=0 ×5 |
| `.ze-editor__filterbar` | `04-editors/gallery/gallery` | 17.4.0 gallery v2 行栈改造后转孤儿,⊘deprecated 至今 | tsx=0 ×5 |
| `.ze-editor__filteractions` | `04-editors/gallery/gallery` | 同上 | tsx=0 ×5 |
| `.ze-param__slider-row` | `02-atoms/data/field-row` | 14.0.0 Slider 改 `.ze-row` 后 deprecated-dead | tsx=0 ×5 |

## 2. 留(有理由,不在 18.0.0 删)

| 项 | 状态 | 为什么留 | 何时清 |
|---|---|---|---|
| `.ze-editor__toolbar` | ⊘defer | **tsx=8,未迁编辑器仍用**(旧 toolbar 迁移期并存) | P4 L4 zone 化全迁后(DESIGN-SYSTEM §6 P4) |
| `.ze-scroll[data-fade="vertical"]` | ⊘deprecated | tsx=0 死属性变体;18.3.0 已剥离其无条件 mask(留 `scrollbar-gutter` 与词表值——删 data-* 值=MAJOR);替代=`ze-scroll-fade-y` + `useScrollAffordance` | 下次 MAJOR 词表清理删 `data-fade` 值 |
| `.ze-share-card/-count/-overlay` | ⊘defer | tsx=0,但为 header-v2 退役保留、share 功能或复用(非 deprecated-dead) | 待用户确认 share 意图后决定删/复用 |
| `--ze-accent-h` / `--ze-accent-c` | 冻结别名 | 0 消费;删 **token** 对下游皮肤风险高于删 class(下游皮肤可能引);accent 自 16.3.0 不再由色相推导,二者恒 252/0.165 静态兼容 | 下次 token MAJOR,确认全生态 0 引用后 |
| `--ze-overlay-soft` | 活/窄用 | 仅 hover/交互叠底,有消费(非死) | 不清 |
| `DESIGN_LAYER_EXCEPTIONS.json`(44 项 ratchet) | 债务棘轮 | L4 ~11 编辑器 React 越级 import 的历史基线,新增即红、只减不增 | 随 P4 逐编辑器 zone 化逐项收缩 |

## 3. 散落文档处置(整理,不删)

| 文件 | 状态 | 处置 |
|---|---|---|
| `PRESET-CONSUMPTION-LADDER.md`(根,27KB) | untracked | 非本轮产物;留原地,待用户确认 track(git add)或标 draft。**不删**。 |
| `FUTURE_STATE.md`(根,22KB) | §2-6 自认部分 stale(2026-06-03 对账) | 有 `doc_status_check`/`ai_interface_check` 兜底防继续漂;§7 是准确"真实待办"。留,下次维护周期与代码对账 §2-6。**不删**。 |

## 4. 旧版本 skill 归档(不删,加指针)

| skill | 状态 | 处置 |
|---|---|---|
| `ZE-seek-find-level-audit`(v1) | legacy(无 01-core,Python audit_engine),仍有 deploy.json | 加 `SUPERSEDED.md` 指向 `-v3`;不删、不动运行态 |
| `ZE-playable-scene-engine`(v1) | legacy(无 01-core),仍有 deploy.json | 加 `SUPERSEDED.md` 指向 `-v3`;不删、不动运行态 |

> `ZE-seek-find-level-packer` / `ZE-batch-image-foundry` 是 **v3-native**(有 01-core),无 v1 需归档,只是命名无 `-v3` 后缀(命名不一致,非旧版本;此处仅备注,不改物理名——改名须用户拍板)。

## 关联
- 分层/引用/关联/类-vs-实例:[DESIGN-SYSTEM.md §2A](../DESIGN-SYSTEM.md)
- 版本编年史:`_contract.json` `_comment` + `MIGRATIONS.md`
