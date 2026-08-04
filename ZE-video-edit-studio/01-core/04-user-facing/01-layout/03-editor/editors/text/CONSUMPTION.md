# Text editor consumption rules

## Layout 声明

```ts
slot('prompt', 'text', ['prompt_doc'], { field: 'body' })
```

## `view_state` 消费规则

- field: 要编辑的字段名，缺省 body
- preview 是 editor 内局部视图状态，不写回 entity

## 组合逻辑

text 通常和 form 配合：form 管元信息，text 管长正文。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
