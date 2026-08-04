/**
 * ActionBarZone — form 编辑器业务动作条(functional zone;manifest id `actionbar`)
 *
 * SlotFormActions(slotAdapters 内联 style 补丁形态)的正规收编:固定骨架(组件画死)=
 * busy 单飞(任一动作执行中全条禁点)+ confirm 守卫 + requiresFields 置灰 + tooltip +
 * running(实体执行中)只置灰删除/替换目标等冲突动作。拓展点只收 FormAction 闭集(命令 id/字段名/枚举,
 * 无回调;zones/types.ts 铁律)。
 * CSS:`.ze-editor__actionbar`(04-editors/form.css,零令牌结构)叠 `.ze-editor__zone`
 * 控制区块几何;视觉走 .ze-button 原子。
 */

import { useContext, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { SlotContext } from '../../lib/SlotContext'
import { renderFormActionButton } from './actionGating'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import { resolveDialog } from './dialogRegistry'
import { ConfirmDialog } from './ConfirmDialog'
import type {
  DispatchFn, EntityDict, EntityTypeSchema, FormActionResultConsumer,
} from '../types'
import type { FormAction } from './types'

export interface ActionBarZoneProps {
  actions: FormAction[]
  entityType: string
  /** active 实体(requiresFields 按它判定;scope:'single' 派发它)。 */
  entity: EntityDict
  /** 多选集(scope:'selection' 派发;不传或空 → 退化为 [entity.id])。 */
  selectionIds?: string[]
  /** requiresFields tooltip 的字段显示名来源(缺回退裸字段名)。 */
  schema?: EntityTypeSchema
  /** 实体执行中(pending/running)→ 仅 conflictsWithRun/danger 动作置灰。 */
  running?: boolean
  dispatch?: DispatchFn
  consumeActionResult?: FormActionResultConsumer
}

export function ActionBarZone({
  actions, entityType, entity, selectionIds, schema, running, dispatch, consumeActionResult,
}: ActionBarZoneProps): ReactNode {
  const t = useT()
  const [busy, setBusy] = useState<string | null>(null)
  const [dialogAction, setDialogAction] = useState<FormAction | null>(null)
  const [confirmReq, setConfirmReq] = useState<{ text: string; run: () => void } | null>(null)
  const hugRef = useScrollAffordance('x')
  // 安全读上下文(useContext 而非 useSlotContext:zone 可能在无 provider 的展示台裸渲染,
  // null 时不门控=字节级不变)。requires_route='image' 的动作据此在未绑凭据时置灰。
  const slotCtx = useContext(SlotContext)
  if (actions.length === 0) return null
  const DialogComp = resolveDialog(dialogAction?.dialog)
  return (
    <div ref={hugRef} className="ze-editor__actionbar ze-editor__zone" data-zone="actionbar" data-role="control" data-layout="hug-scroll">
      {actions.map((a) => renderFormActionButton({
        action: a, entityType, entity, selectionIds, schema, running, dispatch, consumeActionResult,
        busy, setBusy, t, requireFieldsLabel: (fields) => t('slot.requiresFields', { fields }),
        onOpenDialog: (act) => setDialogAction(act),
        onRequestConfirm: (text, run) => setConfirmReq({ text, run }),
        requiresRoute: slotCtx?.commandSpecs?.find((s) => s.id === a.command)?.requires_route,
        aiRoutes: slotCtx?.aiRoutes,
        aiConsent: slotCtx?.aiConsent,
      }))}
      {dialogAction && DialogComp && (
        <DialogComp action={dialogAction} entity={entity} dispatch={dispatch} onClose={() => setDialogAction(null)} />
      )}
      {confirmReq && (
        <ConfirmDialog
          message={confirmReq.text}
          onConfirm={() => { confirmReq.run(); setConfirmReq(null) }}
          onCancel={() => setConfirmReq(null)}
        />
      )}
    </div>
  )
}
