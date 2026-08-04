/**
 * PromptZone — ToolZone 兼容实现(指令输入类·自然语言 → 宿主命令 → 一句结果;03-zones/SPEC §tool)
 *
 * 与 ControlZone(按钮式 ToolZone)并列:ControlZone 收"按钮命令",PromptZone 收"自然语言指令"。
 * 二者都是 ToolZone —— 只触发对当前对象/编辑器的操作,不承载主数据。是把 DialogEditor 的
 * composer(输入框→replyCommand)那套**归纳**成可复用 ToolZone。
 *
 * 发送 → dispatch(command, {[textArg]:输入, type:宿主类型, target:选中id, ...args}) →
 * 从命令返回的 op.external.target_ref.message 读**一句结果**显示(只显最近一句,本地会话态,
 * 不进 Document/不刷屏)。真活在 mod 命令里(可 AI,走 stage)。
 *
 * 拓展点铁律(zones/types.ts §设计铁律):只接命令 id / 字段名 / i18n key,禁裸 onClick/ReactNode,
 * framework 零业务。渲染 .ze-editor__zone[data-role=control][data-function-zone=tool][data-tool=prompt]
 * [data-layout=flex-fill](复用现成 zone 几何 + data-flex,零新 CSS class)。
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'
import { AssetRefInput } from '../primitives/AssetRefInput'
import type { AssetRefDict, AssetUrlFn, DispatchFn } from '../types'

export interface PromptZoneProps {
  /** 派发的宿主命令 id(mod 定义,可 native/stage;唯一业务钩子)。 */
  command: string
  /** 输入文本的参数名,缺省 'text'。 */
  textArg?: string
  /** 占位文案 i18n key(缺省 editor.promptPlaceholder)。 */
  placeholderKey?: string
  /** 宿主编辑器的 entity_type(随命令传,供命令限定范围;装配层补)。 */
  type?: string
  /** 当前选中对象 id(随命令传,作上下文锚;装配层补)。 */
  target?: string
  /** 命令固定附加参数。 */
  args?: Record<string, unknown>
  /** 设了则启用附件:发送时把上传的 asset_ref 并进命令 args[assetArg](照抄 ChatGPT 输入框「传文件」)。
   *  缺省=纯文本(字节等价旧行为);需配 uploadAsset 才出附件落区(seam 闭合,不伪造)。声明式接缝,守铁律。 */
  assetArg?: string
  /** 附件接受格式(原生 file input accept;省=任意),随 assetArg 生效。 */
  accept?: string
  /** 唯一写资产缝:上传二进制 → asset_ref(装配层透传 ctx.uploadAsset);缺则附件落区不出。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  /** asset_ref → 预览 url(装配层透传 ctx.assetUrl);给附件缩略预览。 */
  assetUrl?: AssetUrlFn
  dispatch: DispatchFn
}

type Line = { kind: 'ok' | 'err'; text: string }

interface AppliedResult {
  op?: {
    error?: { code?: string; message?: string }
    external?: { target_ref?: Record<string, unknown>; message?: unknown }
  }
}

export function PromptZone({
  command, textArg = 'text', placeholderKey, type, target, args, assetArg, accept, uploadAsset, assetUrl, dispatch,
}: PromptZoneProps): ReactNode {
  const t = useT()
  const [draft, setDraft] = useState('')
  const [attachment, setAttachment] = useState<AssetRefDict | null>(null)
  const [busy, setBusy] = useState(false)
  const [line, setLine] = useState<Line | null>(null)
  const canAttach = !!uploadAsset && !!assetArg

  const send = async () => {
    const text = draft.trim()
    if ((!text && !attachment) || busy) return
    setBusy(true)
    try {
      const payload: Record<string, unknown> = { ...(args ?? {}), [textArg]: text }
      if (type) payload.type = type
      if (target) payload.target = target
      if (assetArg && attachment) payload[assetArg] = attachment
      const res = (await Promise.resolve(dispatch(command, payload))) as AppliedResult | undefined
      if (res === undefined) {
        setLine({ kind: 'err', text: t('editor.promptFailed') })
      } else if (res.op?.error?.code) {
        setLine({ kind: 'err', text: String(res.op.error.message || t('editor.promptFailed')).slice(0, 200) })
      } else {
        const msg = res.op?.external?.target_ref?.message ?? res.op?.external?.message
        setLine({ kind: 'ok', text: msg ? String(msg).slice(0, 200) : t('editor.promptDone') })
        setDraft('')
        setAttachment(null)
      }
    } catch (e) {
      setLine({ kind: 'err', text: String((e as Error)?.message ?? e).slice(0, 200) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="ze-editor__zone ze-gap"
      data-role="control" data-function-zone="tool" data-tool="prompt" data-layout="flex-fill" data-gap="1"
    >
      {canAttach ? (
        <div data-flex="full">
          <AssetRefInput
            value={attachment} assetUrl={assetUrl} uploadAsset={uploadAsset}
            accept={accept} onChange={setAttachment}
          />
        </div>
      ) : null}
      <textarea
        className="ze-textarea" data-size="sm" data-flex="3"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }}
        placeholder={t(placeholderKey || 'editor.promptPlaceholder')}
        rows={1}
        disabled={busy}
      />
      <button
        type="button" className="ze-button" data-variant="primary" data-size="sm" data-flex="0"
        data-ze-cmd={command}
        disabled={busy || (!draft.trim() && !attachment)} onClick={() => void send()}
      >
        {busy ? <ZeIcon name="loop" size="xs" spin /> : t('editor.promptSend')}
      </button>
      {line ? (
        <div className="ze-text" data-size="xs" data-flex="full"
             data-role={line.kind === 'ok' ? 'tertiary' : undefined}
             data-tone={line.kind === 'err' ? 'danger' : undefined}>
          {line.text}
        </div>
      ) : null}
    </div>
  )
}
