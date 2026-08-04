# Code editor consumption rules

## Layout 声明

```ts
slot('query', 'code', ['query_doc'], { codeField: 'sql', langField: 'language' })
```

## `view_state` 消费规则

- codeField: 代码字符串字段，缺省 code
- langField: 语言标签字段，缺省 lang

## 组合逻辑

code + pipeline 适合做“写查询/运行/看结果”的工作台，执行动作必须在 pipeline 或 form action 里声明。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
