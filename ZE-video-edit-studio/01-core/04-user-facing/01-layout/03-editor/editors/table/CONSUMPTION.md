# Table editor consumption rules

## Layout 声明

```ts
slot('objects', 'table', ['box'], { duplicateCommand: 'entity.duplicate', renameField: 'label' })
```

## `view_state` 消费规则

- columns 来自 entity schema，mod 不在 table 里手写列 DOM
- duplicateCommand: 行复制命令 id，payload 带 type/id/expected_version
- renameField: 允许右键重命名的字段名，提交后走 entity.update

## 组合逻辑

列表-详情结构用 table + form；table 只负责选中哪条记录，form 才负责改字段。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
