# Html editor consumption rules

## Layout 声明

```ts
slot('canvas', 'html', ['page'], {
  htmlField: 'html',          // HTML 源字段(缺省 'html')
  applyCommand: 'entity.update',  // 写回命令(缺省 entity.update,CAS)
})
```

## `view_state` 消费规则

- `htmlField`:实体里承载 HTML 源的字段名;缺省 `html`。
- `applyCommand`:源码写回的命令 id;缺省 `entity.update`。payload 带 `type/id/patches/expected_version`。
- 预览/源码切换是**本地视图态**(默认预览),不进 view_state。
- **沙盒不可配**:iframe `sandbox="allow-scripts"` 由 core 画死,view_state 无任何放宽沙盒的旋钮(安全策略)。

## 组合逻辑

生成型工具:左 `gallery`/`outline`(选页面)+ 右 `html`(预览+源码)。AI 生成命令(mod 自定义,如 `page.generate`)写 `htmlField` → SSE 回流 → iframe 自动重渲。

## 写入规则

源码 draft 在 blur 时若变化 → `dispatch(applyCommand, {type,id,patches:{[htmlField]:draft},expected_version})`。外部(AI/协作)改了 HTML 不覆盖用户源码模式未提交的编辑(预览态才同步 draft←源)。
