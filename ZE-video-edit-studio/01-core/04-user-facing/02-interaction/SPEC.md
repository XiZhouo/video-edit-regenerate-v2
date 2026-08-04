# Interaction — 交互体验横向规则

## 1. 这是什么

ZE 工作台的**交互体验底线** —— 跨所有 UI 层(Header / Footer / editor / zone)统一遵守的规则:点击多久要有反馈、长任务怎么显进度、错误怎么提示、取消按钮何时必须出现、trace-id 怎么贯穿。

业务白话视角:这不是某个具体面板的设计,而是 "所有 ZE 工作台用着都顺手" 的承诺。任何 editor / zone / 菜单都必须遵守。违反任何一条 → 用户体验立刻退化。

interaction 是 **横向**叶子 —— 谁都引用它,它不属于任何 layout 区位。

## 2. 边界与接口契约

### 2.1 跟其他叶子的关系

```
本叶子 (L7) 制定横向交互规则
   ↓ 谁都遵守
L4 Header / L5 Footer / L6 Editor / L3 Zone / L2 UI Module
   ↑
   ↓ 通过本叶子接入横向能力
01-command (取消机制)
02-conflict-resolution (冲突 UI)
03-pipeline (进度 / checkpoint)
05-concurrency (queue position / ETA)
02-operation (trace-id 链路 / 错误事件)
```

> 选择是唯一真相源(指针级):`selection[entity_type]`/`multiSelection[entity_type]` 是跨编辑器
> 联动的**唯一**通道(卡片列表选中 → form 跟随 → 画布活动层,全经本 store;编辑器不私藏选择态、
> 不互发回调)—— 三铁律全文见 03-editor/SPEC §2.1b。
>
> `lastSelected: {entityType, id} | null`(1.28.0,加性):跨类型「最近一次选中」指针——
> `onSelect(type, id)` 非空即更新;`onSelect(type, null)` 且匹配当前类型 → 归 null;
> `onSelectMulti` 末尾同步跟随最后点击。纯会话态、不进 Document/OpLog。
> 消费者:form 槽 `followSelection`(多型 inspector 跨类型跟随,经 SlotContext 透传;
> 解析回退链在 01-layout `lib/inspectorTarget`,follow=false 字节等价旧路径)。
>
> **`currentTime: number`(C0,加性):共享时间游标/playhead(秒)——与 `selection` 同级的
> 第二条跨编辑器联动轴**。`setCurrentTime(t)` 任意编辑器写;绑同轴的编辑器据 `currentTime` 跟随
> (scrub → 预览/关键帧/transport 联动)。**与 selection 同性质:纯会话态、不持久、不进
> Document / OpLog / Operation、不可 undo**(时间游标不是文档数据)。默认 0。经 SlotContext
> 以可选字段 `currentTime?` / `setCurrentTime?` 透传(同 selection 路径);未注入 = 装配层未接
> 共享时间轴,编辑器退回纯本地播放。首个消费者:`media` 编辑器(原生 `<video>` 双向接通 ——
> timeUpdate 提升为共享游标 / 外部改 currentTime 则 seek 回去,防回环见 03-editor `lib/playhead`)。
> 后续 timeline / compare(video)/ keyframe-curve / time-stage 复用同一轴(路线图 C1/C2/C8/C9)。
>
> **`highlights: Record<entityType, string[]>`(F2,加性):跨面板高亮通道 = Rendering Hints 层**——
> 「A 面板选中某实体 → B 面板高亮一组相关对象」。`setHighlights(type, ids)` 任意面板写(`[]` 等价
> 清除)、`clearHighlights(type)` 按类型清;按 entity_type 隔离。**与 selection 同性质:纯会话视觉态、
> 不持久、不进 Document / OpLog / Operation、不可 undo;且与 selection 互不干扰(独立状态,选中态与
> 高亮态可叠加)**。经 SlotContext 以可选字段 `highlights?/setHighlights?/clearHighlights?` 透传
> (同 selection 路径);custom pane 读写入口收束在 01-layout `useEditorSelection()`。首个消费者:
> canvas2d spatial 模式(高亮集内实体画可区分虚线描边);列表类(gallery/table)视觉待 css 层配套。

### 2.2 8 条横向规则(M1 冻结)

| # | 规则 | 阈值 / 形态 | 适用范围 |
|---|---|---|---|
| 1 | **100ms 反馈线** | 用户点击/键盘起算 100ms 内 UI 必有可见反应(高亮 / spinner / focus / 状态变) | 所有交互 |
| 2 | **乐观 UI** | 本地可计算的写操作即时更新 UI,服务器后台确认;失败回滚 | 命名 / 顺序 / 字段值修改;不适用:跨 entity 引用 / 服务器唯一性校验 |
| 3 | **300ms 进度可见线** | 操作超过 300ms 必显进度条或 spinner | 所有 async command |
| 4 | **取消按钮** | 长任务(>2s 预期)必须有 cancel,触发 `03-pipeline.checkpoint` 中断 | pipeline / AI 协作 / 大批量 op |
| 5 | **错误分层显示** | toast(瞬时, 4s 自动消)/ inline(字段级)/ dialog(阻塞)/ footer(被动累积)各自有触发条件 | 所有错误来源 |
| 6 | **队列位置 + ETA** | 等待中任务显示 `第 N/M 个,预计 X 秒` | 走 `05-concurrency.queue` 的所有命令 |
| 7 | **trace-id 全链** | UI 生成 trace-id → API header → command dispatch → operation → oplog 都带同一 id;Footer 显短码 | 所有 command 触发 |
| 8 | **键鼠优先,触屏 M2** | 默认设计针对键盘 + 鼠标;触屏 / 手势 / 笔输入 M2 才加 | M1 范围内所有交互 |

### 2.3 错误分层触发条件

| 形态 | 触发条件 | 视觉(L2 模块) |
|---|---|---|
| **toast** | 瞬时事件,不阻塞工作流,无需用户响应(如:复制成功 / 保存成功)| `.ze-toast`,屏右上角,4s 自动消 |
| **inline** | 单字段校验失败 / 单 entity 局部错误(如:命名重复)| `.ze-callout` 嵌在字段下方,直到用户改正 |
| **dialog** | 必须用户决策才能继续(如:冲突 / 数据丢失风险 / 不可恢复操作前确认)| `.ze-dialog` 模态,阻塞背景 |
| **footer** | 累积性 / 后台错误,不打断用户(如:同步重试中、单条 op 失败)| Footer 错误计数 +1,点开看详情 |

mod 默认走这个分层。特殊场景可自定义 toast 时长 / dialog 标题,但不能改"哪类错误用哪个形态"。

### 2.4 trace-id 链路

```
[UI 生成 uuid] ──触发 command──> [API request header X-Trace-Id]
                                          ↓
                                  [command dispatch 标记 source.trace_id]
                                          ↓
                                  [Operation.trace_id 写入 oplog]
                                          ↓
              ┌────────── Footer 显示 + 折叠面板 trace 链 ──────┘
              ↓
        [如果出错] error event 也带同 trace_id,toast/dialog/footer 都可显
```

trace-id 形态:`uuid4` 完整 + 短码(前 8 位)展示。点击短码复制完整。

### 2.5 跟其他叶子的接口

| 接口方向 | 接口点 |
|---|---|
| → `01-command` | command 触发时生成 trace-id;command 元数据声明是否可取消、预期时长(用于 300ms 进度判定) |
| → `02-conflict-resolution` | 冲突场景 → dialog 形态(M3) |
| → `03-pipeline` | 长任务进度数据源 + cancel checkpoint |
| → `05-concurrency` | queue 位置 + ETA 数据源 |
| → `02-operation` | trace-id 写入 oplog + 错误事件 fan-out 来源 |
| → L1 token | 动效时长(`--ze-dur-*`) + semantic 色 |
| → L2 module | toast / dialog / callout / popover / progress 视觉 |
| → L5 Footer | 错误累积 / trace-id 短码显示 |

### 2.6 mod 重载点

mod 可:
1. 给具体 command 声明"预期时长"(影响 100ms / 300ms 阈值如何应用)
2. 自定义 toast 时长 / dialog 标题
3. 提供业务自定义的"快捷反馈"(如自家品牌的 confetti)

mod **不能**:删除任何 1-8 条规则;改阈值(100ms / 300ms / 2s)。

### 2.7 不在本叶子范围

- 具体动效曲线 / 颜色 / 尺寸 → L1 token + L2 module
- 错误日志持久化 / 上报 → `02-operation` / 后端
- 协作冲突的具体合并策略 → `02-conflict-resolution`
- 触屏 / 手势 / 笔输入 → M2 范围

## 11. 里程碑

- **M1** — 规则 1/3/4/5/7 落地(100ms / 进度 / 取消 / 错误分层 / trace-id);规则 8 默认键鼠
- **M2** — 规则 2 乐观 UI / 规则 6 队列 ETA / 触屏支持
- **M3** — 冲突 UI(对接 `02-conflict-resolution`)

## §3-§10

> Stage B 填:§3 8 条规则各自详细约束(测试如何验证)、§4 trace-id 编码规范、§6 mod 接口、§9 audit gate(每条规则一个 runtime gate,例:GATE-UX-01 100ms-feedback、GATE-UX-04 cancelable-required)、§10 reference 表。
