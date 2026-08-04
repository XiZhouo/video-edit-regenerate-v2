type PromptOptimizationDispatch = (
  commandId: string,
  args: Record<string, unknown>,
) => Promise<unknown> | unknown

/**
 * CustomPane 的共享 dispatch 会把 HTTP/command 错误写进全局错误区后返回 undefined。
 * 提示词优化还要据此维护本地 busy 状态，所以必须把“没有 CommandResult”视为未受理。
 */
export async function submitPromptOptimizationRequest(
  dispatch: PromptOptimizationDispatch,
  payload: Record<string, unknown>,
): Promise<boolean> {
  const result = await dispatch('prompt.optimize', payload)
  return result != null
}
