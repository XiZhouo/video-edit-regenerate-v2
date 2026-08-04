/**
 * TextEditor — `text` editor class
 *
 * 长文/富文本(轻):编辑 1 个 entity 的一个长文本字段(view_state.field,缺省 'body')。
 * 受控件:entity 进、dispatch 出。本地 draft 暂存,blur 时若变化 → entity.update(CAS)。
 * 编辑/预览双态:预览做极简内联渲染(段落 + **粗体** + `代码`),核心是把长文写回 entity。
 * 不引第三方富文本库(core 不打包重库;真要富文本/Markdown 渲染走 mod 或 lazyModule)。
 */

import { useEffect, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ToolRowZone } from '../../zones/ToolRowZone'
import { TextZone } from '../../zones/TextZone'
import { renderMarkdown } from '../../lib/markdown'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type TextEditorProps = {
  entity: EntityDict | null
  /** 编辑器名(layout slot.nameKey 译名;优先显示槽位名,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  field?: string
  dispatch?: DispatchFn
  status?: EntityRunStatus
}

export function TextEditor({ entity, editorName, field = 'body', dispatch, status }: TextEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const [draft, setDraft] = useState('')
  const [preview, setPreview] = useState(false)

  const raw = entity && typeof entity.data[field] === 'string' ? (entity.data[field] as string) : ''
  // entity 换(不同 id 或外部 bump version)→ 重置 draft
  useEffect(() => { setDraft(raw) }, [entity?.id, entity?.version])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!entity) {
    return (
      <EditorFrame editorClass="text" header={<HeaderZone title={editorName ?? t('editor.noText')} />}>
        <EditorFrame.Empty>{t('editor.noText')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const phase = status?.phase
  const commit = () => {
    if (draft === raw) return
    fire('entity.update', { type: entity.type, id: entity.id, patches: { [field]: draft }, expected_version: entity.version })
  }
  const words = draft.trim() ? draft.trim().split(/\s+/).length : 0
  const chars = draft.length

  const objectName = entity.name || entity.id.slice(0, 8)
  const headerTitle = editorName ? `${editorName} · ${objectName}` : objectName
  return (
    <EditorFrame
      editorClass="text"
      header={<HeaderZone title={headerTitle} count={`${t('editor.words')} ${words} · ${chars}`} />}
      zones={[
        <ToolRowZone
          key="tools"
          commands={[{ id: 'preview', kind: 'toggle', viewKey: 'preview', labelKey: 'editor.preview' }]}
          viewState={{ preview }}
          onViewChange={(k, v) => { if (k === 'preview') { if (v) commit(); setPreview(Boolean(v)) } }}
        />,
      ]}
    >
      <TextZone
        variant="text"
        value={draft}
        onChange={setDraft}
        onCommit={commit}
        readOnly={entity.read_only}
        phase={phase}
        error={status?.error}
        placeholder={t('editor.noContent')}
        preview={preview}
        previewHtml={renderMarkdown(draft)}
      />
    </EditorFrame>
  )
}
