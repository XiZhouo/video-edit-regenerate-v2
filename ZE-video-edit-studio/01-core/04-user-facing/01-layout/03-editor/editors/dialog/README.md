# Conversation editor (`conversation`, legacy `dialog`)

代码入口: `DialogEditor.tsx`。目录名保留 `dialog` 是兼容层;公共 editor class 使用 `conversation`。

## 说明

Conversation 是 AI 对话、人机问答和 agent 运行沟通的工作面。它把 message entity 渲成 transcript,底部发送框把用户输入转成 command。terminal/PTY 不再作为独立 editor 推进,而是 conversation 的 terminal profile:输出不进 React state,只作为受控运行视图和 command 交互入口。

## Core 负责

- message 实体按 order/role 渲染为对话流。
- 用户发送消息时先 `entity.add`,再按配置 dispatch `replyCommand`。
- 支持把当前选中的目标 entity 注入 reply args,例如 `page.generate {id, prompt}`。
- 支持后续扩展 question/answer、agent running、retry/stop、terminal profile。

## Core 不负责

- HTML 预览。HTML source/preview/sandbox 归 `html` editor。
- 业务 agent 的推理和产物写回;conversation 只发 command、展示 message/status。
- 直接把 terminal 输出塞进 React state。

## 消费入口

```ts
slot('chat', 'conversation', ['message', 'page'], {
  roleField: 'role',
  textField: 'text',
  orderField: 'ts',
  replyCommand: 'page.generate',
  replyTargetType: 'page',
  replyTargetIdArg: 'id',
  replyTextArg: 'prompt',
  messageLinkField: 'pageId',
})
```
