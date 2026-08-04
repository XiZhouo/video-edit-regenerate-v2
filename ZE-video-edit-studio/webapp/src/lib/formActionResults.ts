/** Pure validation for declarative FormAction result effects.
 *
 * The mod declaration chooses only a closed effect kind.  The downloadable file identity comes
 * from the committed Operation, while ActionReceipt aggregation proves the after-commit writer
 * succeeded.  The two facts must agree in the exact project generation and runtime namespace;
 * no URL, filesystem path, MIME, or endpoint is accepted from mod configuration.
 */

import type { FormActionResultSpec } from '@ze/core/04-user-facing/01-layout/03-editor'

export type ProjectExportDownloadIntent = {
  file: string
  effectKey: string
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

export function projectExportDownloadIntent(
  spec: FormActionResultSpec,
  result: unknown,
  expectedDocumentId: string,
  expectedGeneration: number,
  requestNamespace: string | null,
  currentNamespace: string | null,
): ProjectExportDownloadIntent | null {
  if (spec.kind !== 'download' || !requestNamespace || requestNamespace !== currentNamespace) return null
  if (!expectedDocumentId) return null
  if (!Number.isSafeInteger(expectedGeneration) || expectedGeneration <= 0) return null
  const body = record(result)
  if (!body || body.project_generation !== expectedGeneration || body.action_status !== 'succeeded') return null

  // A download action has one after-commit writer.  Multiple receipts are ambiguous without an
  // action id in the declarative descriptor, so this first version fails closed instead of guessing.
  const actions = body.actions
  if (!Array.isArray(actions) || actions.length !== 1) return null
  const action = record(actions[0])
  if (!action || action.status !== 'succeeded' || typeof action.action_id !== 'string' || !action.action_id) return null

  const operation = record(body.op)
  const external = record(operation?.external)
  const target = record(external?.target_ref)
  const file = target?.file
  const persistence = record(body.persistence)
  const commitId = persistence?.commit_id
  const operationIds = persistence?.operation_ids
  const operationId = operation?.id
  const commandId = operation?.command_id
  if (typeof file !== 'string' || !file || typeof commitId !== 'string' || !commitId
      || typeof operationId !== 'string' || !operationId
      || typeof commandId !== 'string' || !commandId
      || action.action_id !== `${commandId}:${operationId}`
      || persistence?.status !== 'committed'
      || persistence?.document_id !== expectedDocumentId
      || !Array.isArray(operationIds)
      || operationIds.some((id) => typeof id !== 'string' || !id)
      || new Set(operationIds).size !== operationIds.length
      || !operationIds.includes(operationId)) {
    return null
  }
  return {
    file,
    effectKey: `${requestNamespace}\0${commitId}\0${operationId}\0${action.action_id}\0${file}`,
  }
}
