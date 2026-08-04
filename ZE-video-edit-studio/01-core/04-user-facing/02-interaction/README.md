# 02-interaction — 交互体验

> 一句话：用户操作时的反馈细节。

快反馈 (< 100ms 响应) / 乐观 UI / 进度可见 / 取消按钮 / 错误清晰 / 队列位置 / trace-id 全链追踪。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — 完整规则 + 技术文档 (待用户带着填)
- (代码文件待后续添加)

## 怎么用

选择联动是白拿的：框架 App.tsx 调 [`useInteractionStore()`](./useInteractionStore.ts) 把 `selection / multiSelection / onSelect / onSelectMulti` 灌进 SlotContext——mod 只要把两个 slot 绑同一 entity_type，主编辑器（table/gallery）里选中谁，详情编辑器（form/text/review）就显示谁：

```ts
// 02-mod/04-layout.config.ts：两个 slot 绑同一 'item' → 共享 selection['item']，零联动代码
slot('list',   'table', ['item'])
slot('detail', 'form',  ['item'])
```

选择是会话态（不进 Document / OpLog，不可 undo）；多选手势（⌘ toggle / ⇧ range / ⌘A / Esc / 框选）由各 editor 内置。

时间联动也是白拿的：`useInteractionStore()` 同样灌 `currentTime` / `setCurrentTime` 进 SlotContext——这是与 selection 同级的第二条跨编辑器联动轴（共享时间游标 / playhead，秒）。任意编辑器 `setCurrentTime(t)` 写，绑同轴的编辑器据 `currentTime` 跟随（scrub → 预览联动）；与 selection 同性质：纯会话态、不持久、不进 Operation、不可 undo。首个消费者是 `media`（原生 `<video>` 双向接通）。

其余横向规则（100ms 反馈 / 300ms 进度可见 / 错误分层 toast→inline→dialog→footer / trace-id 全链）由框架层执行，mod 的命令一经 dispatch 自动入链；mod 不能删规则、不能改阈值。完整规则见 [SPEC.md](./SPEC.md)。
