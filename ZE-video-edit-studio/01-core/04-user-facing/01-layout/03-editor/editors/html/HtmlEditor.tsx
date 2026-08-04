/**
 * HtmlEditor — `html` editor class
 *
 * 薄 HTML 内容编辑器(text/code 同族):编辑 1 个 entity 的 HTML 字段(view_state.htmlField,
 * 缺省 'html'),预览/源码双态。预览用 **沙盒 iframe**(srcDoc)隔离不可信产物。
 *
 * 安全策略住 core(非 mod 可配):iframe `sandbox="allow-scripts"` 硬编码 —— 刻意不给
 * allow-same-origin(否则产物脚本能触达父应用)/ allow-popups。XSS 隔离是受管 core 的职责
 * (对齐 tutorial-writer XSS 教训),mod 不能经 view_state 放宽。
 * 受控件:entity 进、dispatch 出;源码 draft→CAS(applyCommand,缺省 entity.update);
 * 外部(AI 生成/协作)改了 html 不覆盖用户源码模式未提交编辑。
 */

import { useEffect, useState } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ToolRowZone } from '../../zones/ToolRowZone'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type HtmlEditorProps = {
  entity: EntityDict | null
  /** 编辑器名(layout slot.nameKey 译名;优先显示槽位名,实体名作副标题,与 form/property 一致)。 */
  editorName?: string
  /** HTML 源字段名(缺省 'html')。 */
  htmlField?: string
  /** 写回命令(缺省 'entity.update';CAS 走 expected_version)。 */
  applyCommand?: string
  dispatch?: DispatchFn
  status?: EntityRunStatus
}

export function HtmlEditor({ entity, editorName, htmlField = 'html', applyCommand = 'entity.update', dispatch, status }: HtmlEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const [draft, setDraft] = useState('')
  const [preview, setPreview] = useState(true)   // 默认预览(看渲染结果)

  const raw = entity && typeof entity.data[htmlField] === 'string' ? (entity.data[htmlField] as string) : ''
  // 预览态(或换 entity)→ 同步 draft←raw;源码态(preview=false)不覆盖未提交编辑。
  useEffect(() => { if (preview) setDraft(raw) }, [entity?.id, entity?.version, raw, preview])

  if (!entity) {
    return (
      <EditorFrame editorClass="html" header={<HeaderZone title={editorName ?? t('editor.noHtml')} />}>
        <EditorFrame.Empty>{t('editor.noHtml')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  const commit = () => {
    if (draft === raw) return
    fire(applyCommand, {
      type: entity.type, id: entity.id, patches: { [htmlField]: draft }, expected_version: entity.version,
    })
  }
  const phase = status?.phase
  const busy = phase === 'running' || phase === 'pending'
  const objectName = entity.name || entity.id.slice(0, 8)
  const title = editorName ? `${editorName} · ${objectName}` : objectName

  return (
    <EditorFrame
      editorClass="html"
      header={<HeaderZone title={title} count={busy ? t('editor.generating') : undefined} />}
      zones={[
        <ToolRowZone
          key="tools"
          commands={[{ id: 'preview', kind: 'toggle', viewKey: 'preview', labelKey: 'editor.preview' }]}
          viewState={{ preview }}
          onViewChange={(k, v) => { if (k === 'preview') { if (v) commit(); setPreview(Boolean(v)) } }}
        />,
      ]}
    >
      {preview ? (
        <iframe className="ze-editor__html-frame" title={title} sandbox="allow-scripts" srcDoc={draft} />
      ) : (
        <textarea
          className="ze-code__textarea ze-text ze-pad" data-mono="true" data-size="xs" data-p="2"
          value={draft}
          readOnly={entity.read_only}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          placeholder={t('editor.noContent')}
          spellCheck={false}
        />
      )}
    </EditorFrame>
  )
}
