# thumb 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- **比例 × 流体两轴正交**：`data-thumb-fluid` / `--ze-thumb-card-min-w` 管**宽**（列数/媒体列宽）；`--ze-thumb-media-ratio` 管**高**（媒体盒 高=宽/比，由 ThumbnailGrid 声明或 img onLoad 自然比回写）。两轴互不冲突。
- **三场景三策略**：grid（参考图管理/候选墙）= 方盒 `aspect-ratio:1` + `object-fit:contain`（稳定网格，不裁只留白）；card-list（结果浏览）= 真实比例 `var(--ze-thumb-media-ratio, 1)`；compact-list（快速扫读）= 定行高 `aspect-ratio:1`。不要把一个场景的正确行为当全局默认。
- `.ze-thumb-grid` 必须 `width:100%`：作 flex 子项时防 shrink-to-fit 窄条（容器R5 门禁看守）。
- 已知评审点：`__preview-button` 与 `__score` 同占媒体盒右上角，同时出现会叠——带 score 的图墙如需放大镜，评审时定位次角落。
- 状态必须挂在对象自己身上：优先使用 `data-ui-state`、`data-run-phase`、`aria-invalid`、`disabled` 这类事实属性。
- 同一对象的 normal / empty / running / error / stale / disabled 不能改变外盒尺寸；只改变颜色、描边、透明度、覆盖层或内容可见性。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。
