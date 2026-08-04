/**
 * InspectorSection — inspector / 参数面板的结构 primitive(框架级)
 *
 * SectionTitle / Collapsible / InfoField:editor 的 inspector 区拿它们拼。
 * Collapsible 是"高级控件默认折叠"的 disclosure 机制(实用极简:默认路径不堆控件)。它本身
 * 只接受 controlled 值或显式 stateKey；持久化由上层 DisclosureStateProvider 提供，绝不拿标题/
 * 数组位置猜身份。未提供稳定 key 时保持原有挂载期内存语义。
 * 视觉纯组合 L2 原子(绝对严格分层):段标题 = .ze-bar + .ze-text[role=title];折叠头 =
 *   .ze-disclosure(▸ caret 随 aria-expanded 旋转,标题 = 子 .ze-text[role=title] —— 组头比
 *   它治下的控件更轻,禁拿 ghost 按钮拉全宽充当,见 02-atoms/SPEC §3.1 末);段体 =
 *   .ze-stack[gap][pad];只读属性 = .ze-kv-list。无自造 zone 视觉。
 */

import { useState, type ReactNode } from 'react'
import { useDisclosureItemState } from '@ze/core/04-user-facing/05-preferences'

export function SectionTitle({ children, action }: { children: ReactNode; action?: ReactNode }): ReactNode {
  return (
    <div className="ze-bar" data-edge="top" data-justify="between">
      <span className="ze-text" data-role="title">{children}</span>
      {action ? <span className="ze-text" data-role="tertiary">{action}</span> : null}
    </div>
  )
}

export type CollapsibleProps = {
  title: ReactNode
  defaultOpen?: boolean
  children: ReactNode
  /** Controlled value. When supplied, the owner decides whether/how to persist it. */
  open?: boolean
  onOpenChange?: (open: boolean) => void
  /** Explicit stable declaration id. Never derived from title text or render order. */
  stateKey?: string
}

export function Collapsible({
  title, defaultOpen = true, children, open: controlledOpen, onOpenChange, stateKey,
}: CollapsibleProps): ReactNode {
  const [localOpen, setLocalOpen] = useState(defaultOpen)
  const disclosure = useDisclosureItemState(stateKey, defaultOpen)
  const open = controlledOpen ?? disclosure?.open ?? localOpen
  const toggle = () => {
    const next = !open
    if (controlledOpen === undefined) {
      if (disclosure) disclosure.setOpen(next)
      else setLocalOpen(next)
    }
    onOpenChange?.(next)
  }
  return (
    <div data-open={open ? 'true' : 'false'}>
      <button
        type="button"
        className="ze-disclosure"
        aria-expanded={open}
        onClick={toggle}
      >
        <span className="ze-disclosure__caret ze-icon" data-icon="chevron-right" data-size="xs" aria-hidden="true" />
        <span className="ze-text" data-role="title">{title}</span>
      </button>
      {open && <div className="ze-stack" data-gap="2" data-pad="3">{children}</div>}
    </div>
  )
}

export function InfoField({ label, value }: { label: ReactNode; value: ReactNode }): ReactNode {
  return (
    <div className="ze-kv-list__row">
      <span className="ze-kv-list__key">{label}</span>
      <span className="ze-kv-list__value">{value}</span>
    </div>
  )
}
