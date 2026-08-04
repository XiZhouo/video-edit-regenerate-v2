# icon-map

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

icon-map.css — 图标名 → 令牌映射(L2) — **GENERATED,禁手改**
由 scripts/icons_build.py 从 02-atoms/icons.svg(单一美术源)生成;
改图标 = 改 icons.svg 后重跑 `python3 scripts/icons_build.py`(icon_tokens_check 门禁守同步)。
语义映射 / 迭代规则:02-atoms/icons.md。

## 对外表面

`.ze-icon`
