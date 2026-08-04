# 01-core/01-data-structure — 数据结构

定义"数据长什么样"：项目级容器、可独立选中的数据原子、大文件存储、字段类型校验、容量上限。

## 5 个叶子

1. [01-document/](./01-document/) — Document（顶层数据容器，一个工具一份项目）
2. [02-entity/](./02-entity/) — Entity（项目里的"东西" — 每张图 / 每个物体 / 每条记录）
3. [03-asset/](./03-asset/) — Asset（大文件独立存放，内容寻址，不塞进 Entity）
4. [04-field-types/](./04-field-types/) — Field Types（字段类型系统 + JSON Schema 校验）
5. [05-capacity-limit/](./05-capacity-limit/) — 容量限制（单 Document 实体数上限 / 单 Entity 大小上限 / 大文件阈值）
