/**
 * AiRouteGate — 「没绑就先说」框架不变量的入口门控组件(3.56,AI-CHANNEL-RULES 第 3 步)。
 *
 * 包住任何会消耗 AI 通道的入口:当前路线不可用时,子按钮置灰(pointer-events 拦截),
 * 旁侧给出具体原因 + 「去绑定」一键直达账号弹窗(经 OPEN_CREDENTIALS_EVENT,
 * SettingsMenu 监听打开)。可用时透明透传,零视觉负担。
 *
 * 用法(声明了 requires_route='image' 的命令入口):
 *   const ai = useAiRoutes()
 *   <AiRouteGate state={ai}><button onClick={generate}>生成</button></AiRouteGate>
 */
import React from 'react'
import { useT } from '../05-preferences'
import {
  type AiRoutesState, requestOpenCredentials,
} from '../03-authentication/aiRoutes'
import { targetFromRouteFix } from '../03-authentication/aiSettingsLink'

export function AiRouteGate({ state, route, children }: {
  state: AiRoutesState
  /** 门控哪条路线;缺省 = 服务端当前生效路线(ze.image.route)。 */
  route?: string
  children: React.ReactNode
}): React.ReactNode {
  const t = useT()
  const target = route || state.current
  const info = state.routes.find((r) => r.route === target)
  // 数据未加载/未知路线 → 不拦(驱动层 428 仍兜底,不误伤可用入口)
  if (!state.loaded || !info || info.available) return <>{children}</>
  const reason = t(`aiRoutes.reason.${info.reason ?? 'not_bound'}`)
  return (
    <span className="ze-row" data-gap="2" data-align="center" data-ai-route-blocked={target}>
      <span data-disabled="true" style={{ opacity: 0.45, pointerEvents: 'none' }}>{children}</span> {/* layer-guard-allow-inline:门控置灰是运行时禁用语义,非视觉装饰 */}
      <span className="ze-text" data-size="xs" data-ink="tertiary">{reason}</span>
      {info.fix?.action === 'bind' && (
        // 带上 fix 里的 provider/capability:服务端已判过该绑什么,别让弹窗又停在默认页
        <button type="button" className="ze-link"
          onClick={() => requestOpenCredentials(targetFromRouteFix(info.fix))}>
          {t('aiRoutes.goBind')}
        </button>
      )}
    </span>
  )
}
