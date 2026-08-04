/**
 * LayoutRenderer — LayoutTree 递归渲染器(SPEC §2.2 / §2.3)
 *
 * 渲染三种节点:
 *   - Split  : 两块 + 中间可拖分割条(改 ratio)
 *   - Tabs   : tab header + 当前 active 子节点
 *   - Slot   : 查 EDITOR_CLASSES,据 entity_bindings 从 SlotContext 取数据渲染 editor
 *
 * onChange 传播:每个交互节点拿 root + onChange + 自己的 id,改动时用 layoutOps 对
 * root 做 by-id 操作生成新 root,调 onChange(root) 一路冒到装配层。不用 path 透传,
 * 避免深层 stale-path。
 *
 * 拖拽复用 Footer 模式:draggingRef 同步真值 + dragging state 仅驱动 CSS;
 * 最新 root/onChange 放 latest ref,拖拽 effect 不依赖它们,避免每帧重注册监听。
 */

import {
  useCallback, useEffect, useRef, useState, type ReactNode, type CSSProperties,
} from 'react'

import { useT } from '@ze/core/04-user-facing/05-preferences'

import { moveTab, setActiveTab, setRatio } from './lib/layoutOps'
import { renderSlot } from './lib/slotAdapters'
import { useSlotContext } from './lib/SlotContext'
import { tabLabel } from './lib/tabLabel'
import type { FormActionResultConsumer } from './03-editor'
import {
  MIN_SLOT_H, MIN_SLOT_W,
  type EditorSlotNode, type LayoutNode, type SplitNode, type TabsNode,
} from './types'

type NodeProps = {
  node: LayoutNode
  root: LayoutNode
  onChange: (tree: LayoutNode) => void
  /** Host-only capability. It is threaded through managed components as a prop and is never
   *  placed in React context or forwarded to a custom renderer subtree. */
  formActionResultConsumer?: FormActionResultConsumer
}

export function LayoutRenderer(
  { tree, onChange, formActionResultConsumer }: {
    tree: LayoutNode
    onChange: (t: LayoutNode) => void
    formActionResultConsumer?: FormActionResultConsumer
  },
): ReactNode {
  return (
    <div className="ze-layout-root">
      <NodeView
        node={tree}
        root={tree}
        onChange={onChange}
        formActionResultConsumer={formActionResultConsumer}
      />
    </div>
  )
}

function NodeView({ node, root, onChange, formActionResultConsumer }: NodeProps): ReactNode {
  switch (node.kind) {
    case 'split': return <SplitView node={node} root={root} onChange={onChange}
      formActionResultConsumer={formActionResultConsumer} />
    case 'tabs':  return <TabsView node={node} root={root} onChange={onChange}
      formActionResultConsumer={formActionResultConsumer} />
    case 'slot':  return <EditorSlotView node={node} formActionResultConsumer={formActionResultConsumer} />
  }
}


// ============================================================
// Split
// ============================================================

function SplitView(
  { node, root, onChange, formActionResultConsumer }: {
    node: SplitNode
    root: LayoutNode
    onChange: (t: LayoutNode) => void
    formActionResultConsumer?: FormActionResultConsumer
  },
): ReactNode {
  const containerRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ rect: DOMRect } | null>(null)
  const [dragging, setDragging] = useState(false)

  // 最新值放 ref:拖拽 effect 只依赖 dragging,不因 root 每帧变化而重注册
  const latest = useRef({ root, onChange, direction: node.direction, id: node.id })
  latest.current = { root, onChange, direction: node.direction, id: node.id }

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const el = containerRef.current
    if (!el) return
    dragRef.current = { rect: el.getBoundingClientRect() }
    setDragging(true)
  }, [])

  useEffect(() => {
    if (!dragging) return

    const onMove = (e: MouseEvent) => {
      const d = dragRef.current
      if (!d) return
      const { rect } = d
      const { root: r0, onChange: oc, direction, id } = latest.current
      let r: number
      if (direction === 'horizontal') {
        const minR = MIN_SLOT_W / rect.width
        const maxR = 1 - minR
        r = (e.clientX - rect.left) / rect.width
        r = minR > maxR ? 0.5 : Math.min(maxR, Math.max(minR, r))
      } else {
        const minR = MIN_SLOT_H / rect.height
        const maxR = 1 - minR
        r = (e.clientY - rect.top) / rect.height
        r = minR > maxR ? 0.5 : Math.min(maxR, Math.max(minR, r))
      }
      oc(setRatio(r0, id, r))
    }

    const onUp = () => {
      dragRef.current = null
      setDragging(false)
    }

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [dragging])

  return (
    <div className="ze-split" data-direction={node.direction} ref={containerRef}>
      <div
        className="ze-split__pane"
        data-pane="primary"
        style={{ '--split-basis': `${node.ratio * 100}%` } as CSSProperties}
      >
        <NodeView node={node.first} root={root} onChange={onChange}
          formActionResultConsumer={formActionResultConsumer} />
      </div>

      <div
        className="ze-split__divider"
        data-dragging={dragging ? 'true' : 'false'}
        role="separator"
        aria-orientation={node.direction === 'horizontal' ? 'vertical' : 'horizontal'}
        onMouseDown={onMouseDown}
      />

      <div className="ze-split__pane" data-pane="secondary">
        <NodeView node={node.second} root={root} onChange={onChange}
          formActionResultConsumer={formActionResultConsumer} />
      </div>
    </div>
  )
}


// ============================================================
// Tabs
// ============================================================

function TabsView(
  { node, root, onChange, formActionResultConsumer }: {
    node: TabsNode
    root: LayoutNode
    onChange: (t: LayoutNode) => void
    formActionResultConsumer?: FormActionResultConsumer
  },
): ReactNode {
  const t = useT()
  const active = Math.min(Math.max(node.active_index, 0), node.children.length - 1)
  const child = node.children[active]
  // 拖拽源序号放 ref(同 SplitView 模式:拖拽真值走 ref,不触发额外渲染)
  const dragFrom = useRef<number | null>(null)

  return (
    <div className="ze-tabs">
      <div className="ze-tabs__headers" role="tablist">
        {node.children.map((c, i) => (
          <button
            key={c.id}
            type="button"
            role="tab"
            className="ze-tab"
            data-active={i === active ? 'true' : 'false'}
            aria-selected={i === active}
            draggable
            onDragStart={() => { dragFrom.current = i }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              const from = dragFrom.current
              dragFrom.current = null
              if (from !== null && from !== i) onChange(moveTab(root, node.id, from, i))
            }}
            onDragEnd={() => { dragFrom.current = null }}
            onClick={() => onChange(setActiveTab(root, node.id, i))}
          >
            {tabLabel(c, t)}
          </button>
        ))}
      </div>
      <div className="ze-tabs__body" role="tabpanel">
        {child && <NodeView node={child} root={root} onChange={onChange}
          formActionResultConsumer={formActionResultConsumer} />}
      </div>
    </div>
  )
}

// ============================================================
// EditorSlot
// ============================================================

function EditorSlotView({ node, formActionResultConsumer }: {
  node: EditorSlotNode
  formActionResultConsumer?: FormActionResultConsumer
}): ReactNode {
  // 所有 editor_class → props 的接线集中在 slotAdapters.renderSlot(开/闭原则:
  // 新增 editor 不改本渲染器)。本组件只负责 slot 外壳。
  // 〔运行时分屏/关闭的 slot chrome 已按用户裁决移除(2026-06-13,SPEC §2.3):
  //   槽位增删由 mod default layout 静态声明,不提供运行时入口。〕
  const ctx = useSlotContext()
  return <div className="ze-slot">{renderSlot(node, ctx, formActionResultConsumer)}</div>
}
