/**
 * tableColumns — 表格列集组装(纯函数;TableZone 消费)
 *
 * 名称列:Entity.name 是框架级身份字段(每 entity 恒有;导入/新建时即填;搜索恒匹配它),
 * 但表格列派生只出 schema.properties(data 字段)—— 行在表格里没有身份,全参数字段的
 * 类型整表不可辨识(image-cleanup 图片列表只剩"噪点评分 1.11",2026-07-06 说明书试点实测)。
 * 故 core 恒定前置 field:'name' 身份列。field 语义与 cardlist readCardField 同源
 * ('name' = entity 顶层,TOP_FIELDS 先于 data 字段)。
 *
 * 去重:列集已有 name / data.name 列(mod 自带身份列)→ 不重复前置。
 * 逃生舱:mod 在 layout slot view_state 声明 nameColumn:false 关闭(如列集已有
 * required title 这类业务身份列、不想双列的表)。
 */
import type { ListColumn } from './types'

/** 框架身份列(表头走 i18n editor.name;取值走 readCardField → entity.name)。 */
export const NAME_COLUMN: ListColumn = { field: 'name', labelKey: 'editor.name' }

export function withNameColumn(columns: ListColumn[], nameColumn = true): ListColumn[] {
  if (!nameColumn) return columns
  if (columns.some((c) => c.field === 'name' || c.field === 'data.name')) return columns
  return [NAME_COLUMN, ...columns]
}
