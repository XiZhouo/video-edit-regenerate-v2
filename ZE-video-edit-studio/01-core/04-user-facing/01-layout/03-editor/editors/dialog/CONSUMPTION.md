# Conversation editor consumption rules

## Layout 声明

```ts
slot('chat', 'conversation', ['message', 'page'], {
  roleField: 'role',
  textField: 'text',
  orderField: 'order',
  userRole: 'user',
  replyCommand: 'page.generate',
  replyTargetType: 'page',
  replyTargetIdArg: 'id',
  replyTextArg: 'prompt',
  messageLinkField: 'pageId',
})
```

`dialog` 仍是兼容 alias,新布局不要继续使用。

## `view_state` 消费规则

- `roleField` / `textField` / `orderField`:消息字段映射;缺省 `role` / `text` / `order`。
- `userRole`:发送方 role 值,缺省 `user`,气泡靠右 accent;其余靠左。
- `replyCommand`:可选;发送后额外派发触发 AI 回复/生成。
- `replyTextArg`:输入文本进入 replyCommand 的参数名,缺省 `text`。
- `replyTargetType` / `replyTargetIdArg`:把当前目标 entity id 注入 replyCommand。
- `replyArgs`:固定附加参数。
- `messageLinkField`:发送 message 时写入目标 entity id,用于 message 与产物关联。

草稿输入是本地态,不进 view_state。

## 组合逻辑

对话 + 画布/产物:左 `conversation`(发指令),右 `html` / `canvas2d` / `cardlist`(AI 产物)。message 与产物经 selection / parentId / messageLinkField 联动,互不 import。

terminal/PTY 是 conversation profile,不是独立 editor。

## 写入规则

发送 = `dispatch('entity.add', {type, name, data:{[roleField]:userRole,[textField]:text,[orderField]:next}})`。`replyCommand` 由 mod 实现“起生成 + 把回复写回 message/target entity”。AI 回复经 SSE entity_change 回流自动重渲。
