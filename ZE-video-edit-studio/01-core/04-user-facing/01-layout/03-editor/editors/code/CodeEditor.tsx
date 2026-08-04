/**
 * CodeEditor — `code` editor class
 *
 * 结构化文本(轻):编辑 1 个 entity 的代码字段(view_state.codeField,缺省 'code')+ 行号槽 +
 * 语言标签(langField,缺省 'lang')。受控件:draft 暂存,blur 时变化 → entity.update(CAS)。
 * 不打包 CodeMirror/Monaco(core 不打包重库;语法高亮属重库,留 lazyModule/mod 后补)——
 * 本编辑器提供等宽 + 行号 + 语言标签的"可编辑代码"基座,与其余编辑器同成熟度档。
 */

import { useEffect, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { TextZone } from '../../zones/TextZone'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type CodeEditorProps = {
  entity: EntityDict | null
  /** 编辑器名(layout slot.nameKey 译名;优先显示槽位名,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  codeField?: string
  langField?: string
  dispatch?: DispatchFn
  status?: EntityRunStatus
}

export function CodeEditor({ entity, editorName, codeField = 'code', langField = 'lang', dispatch, status }: CodeEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const [draft, setDraft] = useState('')

  const raw = entity && typeof entity.data[codeField] === 'string' ? (entity.data[codeField] as string) : ''
  const lang = entity && typeof entity.data[langField] === 'string' ? (entity.data[langField] as string) : ''
  useEffect(() => { setDraft(raw) }, [entity?.id, entity?.version])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!entity) {
    return (
      <EditorFrame editorClass="code" header={<HeaderZone title={editorName ?? t('editor.noCode')} />}>
        <EditorFrame.Empty>{t('editor.noCode')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const phase = status?.phase
  const commit = () => {
    if (draft === raw) return
    fire('entity.update', { type: entity.type, id: entity.id, patches: { [codeField]: draft }, expected_version: entity.version })
  }
  const lineCount = draft.length ? draft.split('\n').length : 1
  const lineLabel = `${lineCount} ${t('editor.lines')}`

  const objectName = entity.name || entity.id.slice(0, 8)
  const headerTitle = editorName ? `${editorName} · ${objectName}` : objectName
  return (
    <EditorFrame
      editorClass="code"
      header={<HeaderZone title={headerTitle} count={lang ? `${lang} · ${lineLabel}` : lineLabel} />}
    >
      <TextZone
        variant="code"
        value={draft}
        onChange={setDraft}
        onCommit={commit}
        readOnly={entity.read_only}
        phase={phase}
        error={status?.error}
        placeholder={t('editor.noContent')}
      />
    </EditorFrame>
  )
}
