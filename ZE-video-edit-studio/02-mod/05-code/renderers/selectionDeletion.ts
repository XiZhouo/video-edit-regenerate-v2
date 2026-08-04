type Dict = Record<string, any>
type Picked = { type: 'clip' | 'annotation'; id: string } | null

export type DeletionCommand = {
  commandId: 'annotation.remove' | 'entity.delete'
  payload: Record<string, unknown>
}

function isTypingTarget(target: unknown): boolean {
  const element = target as { tagName?: unknown; isContentEditable?: unknown } | null
  const tag = String(element?.tagName || '').toUpperCase()
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
    || element?.isContentEditable === true
}

/** Delete / Backspace 都复用时间轴已有的删除命令；输入文字时不拦截。 */
export function deletionForKey(
  event: { key: string; target: unknown },
  picked: Picked,
  annotations: Dict[],
  clips: Dict[],
): DeletionCommand | null {
  if ((event.key !== 'Delete' && event.key !== 'Backspace') || isTypingTarget(event.target)) {
    return null
  }
  if (!picked) return null
  if (picked.type === 'annotation') {
    return annotations.some((entity) => entity.id === picked.id)
      ? { commandId: 'annotation.remove', payload: { id: picked.id } }
      : null
  }
  const clip = clips.find((entity) => entity.id === picked.id)
  return clip
    ? {
        commandId: 'entity.delete',
        payload: { type: 'clip', id: clip.id, expected_version: clip.version },
      }
    : null
}
