# Footer — 工作台底部固定栏

## 1. 这是什么

ZE 工作台**屏幕最底部的固定栏** —— 容纳运行时**状态可见性** + **当前活跃 pipeline 的 lifecycle 管控**:服务器状态、协作模式、错误计数、trace-id 摘要、最近 op 日志、各 pipeline 的实时进度与 pause/resume/cancel 控制。

业务白话视角:用户低头瞥一眼 Footer 就知道"现在系统在干什么、有没有出错、跟服务器通讯顺不顺",并能**直接在 Footer 里**暂停 / 停止 / 继续当前跑着的某条 pipeline —— 不用切到别的面板。需要细节时点开把手,展开到 240px 默认面板看最近 op + 每个 pipeline 的日志 + 错误明细。

Footer 跟中区 [editor](../03-editor/SPEC.md) 和顶部 [Header](../01-header/SPEC.md) **平级**,共同构成 layout 三大区。

**重要边界**:Footer **不做业务数据 mutation**(改 entity / 改 doc / 新建 doc),那是 editor 和 Header 的事;Footer **可以做 pipeline lifecycle 控制**(pause/resume/cancel),因为这些动作的判断依据正是 Footer 本身显示的状态(进度、日志、错误)—— 把控制按钮放在状态旁是最自然的位置。

**重要区分**:本叶子的 Footer 是**工作台级**的 Footer(layout 顶层);[L3 zone](../../../../03-css-rules/03-zones/SPEC.md) 里也有一个 `footer` zone,那是 **editor 内的本地状态条**,两者不混。
【footer的日志要把最新的放在上面，并且显示30条就行，其他滚动看。在这下面分别显示每个pipeline的日志，并且这些管线的暂停、停止、继续按钮都在这里记录着。】
## 2. 边界与接口契约

### 2.1 跟其他叶子的关系

```
Footer (本叶子, L5)
   |
   ├─ 订阅 02-operation (最近 op 摘要,只读)
   ├─ 订阅 02-data-flow/01-realtime-sync (同步状态:在线/离线/同步中/冲突,只读)
   ├─ 订阅 05-concurrency (pending tasks / queue position,只读)
   ├─ 订阅 03-pipeline (各 pipeline 的状态 / 进度 / 日志,只读)
   ├─ 订阅 L7 Interaction (错误事件 / trace-id,只读)
   ├─ 调用 03-pipeline (pause / resume / cancel,**写,有限范围**)
   └─ 内部组件全部用 L2 .ze-* (status-tile / chip / progress / button / icon-button / popover / kv-list)
```

订阅 = 只读 + 自动 re-render;调用 = dispatch command,**仅限 pipeline lifecycle 三个动作**(pause / resume / cancel),不允许其他 command。

### 2.2 默认显示(折叠态,24px 高)

```
┌────────────────────────────────────────────────────────────────┐
│ ● skill-name │ ⟳ syncing │ ▶ 2 tasks │ ⚠ 0 errors │ #trace-... │
└────────────────────────────────────────────────────────────────┘
```

| segment | 显示什么 | 颜色 |
|---|---|---|
| 服务器状态 | `online` / `offline` / `connecting` | `--ze-semantic-success` / `danger` / `info` |
| skill 标识 | 当前 skill 名 + 数据目录路径(hover 显示完整) | `--ze-ink-secondary` |
| 同步状态 | `in-sync` / `syncing` / `delayed` / `conflict` / `offline` | semantic 色;折叠态不常驻显示 `in-sync` |
| 进度 | 最新 active pipeline / task 数 + 当前进度(若有 long task) | accent |
| 错误计数 | 自上次清零起的 error count | `warn` / `danger` |
| trace-id | 最近一次 command 的 trace-id 短码(点击复制) | `ink-tertiary` |

折叠态 segment 行 = toggle 把手(click/双击);**拉伸走顶缝手柄**(见 §2.5,与编辑器分屏同语言)。

### 2.3 展开态(默认 240px,用户可拖)

展开后 Footer 自上而下三段:

```
┌── (把手 24px,仍然是折叠态那条 segment,可继续点/拖) ───────────┐
├── [最近 op 日志] ────────────────────────────────────────────│
│   最新在顶,固定显示 30 条,超出滚动                              │
├── [pipeline 列表] ──────────────────────────────────────────│
│   每条活跃 pipeline 一块:                                      │
│   <name>  <stage>  <progress%>  [⏸暂停] [⏹停止] [▶继续]      │
│   下方一行折叠日志,可点开看该 pipeline 全量日志                  │
├── [其他详情(默认折叠)] ────────────────────────────────────│
│   同步详情 / 错误列表 / trace-id 链(各自一个可展开 tab)        │
└─────────────────────────────────────────────────────────────┘
```

| 区段 | 内容来源 | 写权限 |
|---|---|---|
| 最近 op 日志 | `02-operation.OpLog.tail(30)`,倒序 | 只读 |
| pipeline 列表 | `03-pipeline.runs.active()` 实时 | **写:dispatch pipeline.pause / resume / cancel** |
| 同步详情 | `02-data-flow/01-realtime-sync.state` | 只读 |
| 错误列表 | L7 错误事件流(可点跳 dialog 详情) | 只读 |
| trace-id 链 | 当前 trace-id 全链(UI → API → command → oplog) | 只读 |

展开面板**不是**完整 oplog viewer / task manager —— 那些是独立面板。Footer 只显示最近 30 条 op + 当前活跃 pipeline + 当前 trace-id。

### 2.4 哪些事件入 Footer

| 事件来源 | 入 Footer 的形态 |
|---|---|
| Operation 完成 | 进入最近 op 顶部 + 状态条 op 计数 +1 |
| Operation 失败 / error | 错误计数 +1 + segment 变 danger 色 + 入错误列表 |
| pipeline 启动 | pipeline 列表新增一块,显示 stage / progress / 控制按钮 |
| pipeline 进度更新 | 该 pipeline 块进度条更新,结构化执行日志追加(`log_events` 优先,旧 `log_lines` 回退) |
| pipeline 结束 (success/cancel/error) | 该 pipeline 块状态变色;若 success 短暂保留后移除;若 error 留住等用户确认 |
| 同步状态变化 | 同步 segment 变色 + 出现在展开面板"同步详情" |
| trace-id 切换 | 显示新 trace-id 短码;旧的进展开面板"trace 链" |

**不入 Footer**:editor 内的局部交互(选区变化、hover) —— 这些不是"系统级状态"。

### 2.5 高度策略 + 拖拽行为

**折叠态**:1×row-h,整条贴屏幕底;segment 行 = **toggle 把手**(click/双击开合,纯按钮语义)。

**拉伸(统一手柄语言,core 1.32.0)**:Footer 与中区之间的**空气缝是唯一拉伸位**(`.ze-footer__resizer`),
视觉/交互逐字对齐编辑器分屏的 `.ze-split__divider`:透明缝,hover/拖拽时中心点亮 2px accent 胶囊线,
命中区 ±3px,cursor ns-resize;**折叠/展开态常显**——折叠态从缝上拖 = 自动展开(行为不变,载体从把手移到缝)。
结构上缝是 footer 的**前置兄弟** flex 项(缝即元素,同 divider 模型;负 margin 叠进中区底 padding,净占位 0)
——不可做 `.ze-footer` 子元素:footer 的 `overflow:clip` 会把悬出部分连绘制带命中一起裁掉。
窄栏(`@container ze-app ≤768px`)随 `.ze-split__divider` 同断点隐藏。
把手行内不再有任何拉伸暗示(旧「展开态 ns-resize + 灰横杠」已删)。Header 与中区之间**无**此手柄:
header 高度是红线(固定 1×row-h 不可拖),不存在可拖边界。**默认初始展开高度 240px**,
用户可拖到任意 `≥折叠高`(回到折叠)且 `≤(屏幕高 - Header高 - 中区最小高)` 的高度。

```
折叠态                     展开态(默认 240px,可拖)
┌─────────────┐             ┌─────────────┐
│   Header    │             │   Header    │
├─────────────┤             ├─────────────┤
│             │             │             │
│   中区      │             │   中区      │
│             │             │   (压缩)    │
│             │             │             │
├─────────────┤    →        ├─────────────┤ ← 把手(可拖)
│ [把手 24px] │             │ [把手 24px] │
└─────────────┘             ├─────────────┤
                            │  展开区     │
                            │  ~216px     │
                            │  (可拖改高)  │
                            └─────────────┘
```

关键约束:

- **挤压不浮**:Footer 升起 = 把中区高度挤压,**不**浮在中区之上。中区始终完整可见。
- **触发中区响应式**:中区 EditorSlot 被挤窄 / 压低 → editor 自动按 [L6 §2.8 尺寸预设](../03-editor/SPEC.md#28-尺寸预设响应式) 切档(原 md 可能变 sm,原 lg 可能变 md)。
- **把手永远在 Footer 顶**:展开 / 折叠 / 拖动过程中,把手始终是 Footer 最顶一行;拖动它即拖动 Footer 整体高度。
- **拖到折叠态高度**:再拖一次 = 完全折叠(回 24px);双击把手也是同样的 toggle。
- **持久化**:用户调整的高度走 [01-layout §2.5](../SPEC.md) 的 LayoutTree 持久化。

Header(28px,固定)+ Footer(动态)+ 中区(剩余)= 100vh。

【Footer 展开后，是折叠时候的条往上升，并且升起后可以自由拖动，不是固定的。升起后，是挤掉中区的区域，不是浮在中区上面，你理解下。所以中区的编辑器也会被顶的缩起来】

### 2.6 mod 重载点

mod 可在 `02-mod/04-layout.md` 声明:

1. **追加 segment**:折叠态加业务自定义状态显示(协作中人数、当前批次等)
2. **覆盖 segment**:替换默认显示(如把 skill 标识改成项目元数据)
3. **展开面板加 tab**:业务自定义 tab 进入 "其他详情" 区
4. **追加 pipeline lifecycle 按钮**:为自家 pipeline 类型自定义额外的 lifecycle 控制(如 "重试上一步")—— **仅限 pipeline lifecycle 范畴,不允许业务数据 mutation 按钮**

mod **不能**:改 Footer 高度档位 / 把 Footer 改成业务 mutation 入口(那是 editor + Header 的事) / 删除 pipeline 控制按钮(它们是基础设施)。

### 2.7 跟其他叶子的接口

| 接口方向 | 接口点 |
|---|---|
| ← `02-operation` | 订阅 OpLog 末尾 30 条(只读) |
| ← `02-data-flow/01-realtime-sync` | 订阅 sync state(只读) |
| ← `05-concurrency` | 订阅 queue + active tasks(只读) |
| ← `03-pipeline` | 订阅 runs.active() 实时(只读) |
| → `03-pipeline` | dispatch `pipeline.pause` / `pipeline.resume` / `pipeline.cancel`(**写,仅限这三个**) |
| ← L7 Interaction | 订阅错误事件流 + trace-id 链 |
| → L1 token | 颜色 / 间距 / 高度全部走 token |
| → L2 module | status-tile / chip / progress / button / icon-button / popover / kv-list |

### 2.8 不在本叶子范围

- 完整 oplog viewer(浏览全部历史) → 独立面板,M2/M3
- 完整 task / pipeline manager(批量管理、历史回溯、重新调度) → 独立面板,M2/M3
- 错误详情弹层(看 stack trace / 重试 / 上报) → L7 dialog
- **业务数据 mutation**(改 entity / 改 doc / 新建 doc) → editor / Header,**Footer 一律不做**
- 业务命令触发(导出、发送、发布等) → Header / editor toolbar
- 协作多人头像列 → Header 右段

## 11. 里程碑

- **M1** — 6 segment 折叠态 + 把手上升展开 + 拖拽改高 + 最近 op 30 条(倒序滚动)+ pipeline 列表(进度 + pause/resume/cancel)+ 挤压触发中区响应式
- **M2** — mod 追加 segment / 展开 tab + mod 自定义 pipeline lifecycle 按钮 + 同步详情 / 错误列表 / trace-id 链完整展开
- **M3** — 协作活动流 / 批次进度浮窗 / pipeline 历史回溯入口

## §3-§10

> Stage B 填:§3 各 segment 详细规则、§4 数据订阅频率(节流避免狂闪)、§6 mod 追加接口、§9 audit gate(Footer 仅允许 dispatch `pipeline.{pause,resume,cancel}`,其他 command 一律拒)、§10 reference 表。
