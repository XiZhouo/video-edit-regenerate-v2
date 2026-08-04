/**
 * assistantBus.ts — 单一 AI 助手的进程内协调总线(与 guideBus/aiActivityBus 同型)。
 *
 * 【单一助手原则(2026-07-19 用户定稿)】整个页面只有**一个**AI 助手实体:精灵变形体
 * (GuideLayer/spriteEngine)是它唯一的身体——点击它=打开对话(CopilotDock 面板),
 * 教学/回执/确认拉起=它飞出去演。CopilotDock 不再渲染自己的小球,只贡献对话面板;
 * 两个组件经本总线协调,互不 import(面板开合/停靠边/忙碌态/教程入口四种帧)。
 *
 * 本总线只搬 UI 协调事实,从不触碰命令——对话走 /api/ai/chat,教学走 guided_tour,
 * 执行永远在命令总线(source=ai + ACL/CAS + 确认闸门)。
 */

export type AssistantFrame =
  | { type: 'toggle_chat' }
  | { type: 'chat_state'; open: boolean; side: 'left' | 'right' }
  | { type: 'busy'; busy: boolean }
  | { type: 'open_tours' }
  // §7.3 变更卡「定位到实体」:点变更卡里的实体 → 请求编辑器把它设为选中(纯 UI 协调,不触命令)。
  | { type: 'reveal_entity'; entityType: string; entityId: string; field?: string }
  // §7.3 W1-1「面板由精灵长出」:精灵把自己当前停泊中心(视口坐标)播给 AssistantRoot,
  // 后者写成 --ze-assistant-origin-x/y,面板据此设 transform-origin ——
  // 于是展开动画的起点**就是精灵所在的那个点**,而不是面板自己的某个角。
  // 这是「分身」与「同一个东西长出来」在实现上的全部区别:少了这一帧,
  // 面板只能凭空出现在固定位置。
  | { type: 'sprite_pos'; x: number; y: number }
  // §7.3 W1-5:待确认拉起转投可读面板。确认帧本来就到得了前端(pending_confirmation SSE →
  // useAiActivity),但它只渲染成精灵气泡上的两个按钮;0721 §5.6 与 §7.3「主要信息在可读面板中
  // 展示」要的是面板里那张说清「做什么 / 影响 / 为什么要确认」的卡。合并成一棵树之后,
  // 精灵把它接到的 payload 转投给面板,由 ConfirmCard 渲染;气泡退回只做提示。
  | { type: 'pending_confirm'; payload: Record<string, unknown> | null }
  // 面板上的三个动作回投给精灵侧执行(批准走既有网页 nonce 通道,AI 拿不到 nonce)。
  | { type: 'confirm_action'; action: 'approve' | 'later' | 'cancel'; nonce: string }
  // §7.3 W1-4:轮内阶段推进(understanding/reading/tool/waiting/done),驱动过程卡五段。
  // 由 useDocument 从 /api/events 的 assistant_stage 帧转投;tool 段带命令名,
  // 过程卡才说得出「正在调用 批量生成」而不是干巴巴一句「处理中」。
  | { type: 'stage'; stage: string; tool: string }
  // W1-1 自查补:面板矩形。精灵的 `panel` 停靠形态要拿它算停靠点
  //(spriteEngine.parkPoint 的 panel 分支 + update 里 `mode === 'rest' || !rect` 的早退)——
  // 不给 rect,panel 分支根本到不了,形态开了也等于没开。null = 面板收起。
  | { type: 'panel_rect'; rect: { top: number; left: number; width: number; height: number } | null }

type Listener = (frame: AssistantFrame) => void

const listeners = new Set<Listener>()

export function publishAssistant(frame: AssistantFrame): void {
  for (const listener of Array.from(listeners)) {
    try {
      listener(frame)
    } catch {
      /* 单个订阅者抛错不影响其余(纯 UI 协调) */
    }
  }
}

export function subscribeAssistant(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
