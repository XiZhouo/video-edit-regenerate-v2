import type { HeaderConfig } from '@ze/core/04-user-facing/01-layout/01-header'

export type ModHeaderConfigSource = {
  skillName?: string
  /** Canonical machine-readable header/manual declaration. */
  headerConfig?: HeaderConfig
  /** Legacy optional seam retained for already-generated mods. */
  manualHtml?: string
}
function nonEmpty(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

/**
 * Resolve one immutable config object for every header/manual surface.
 * Canonical `headerConfig.manual_html` wins; legacy `manualHtml` is fallback;
 * old mods declaring neither retain the framework default manual URL.
 */
export function resolveModHeaderConfig(source: ModHeaderConfigSource): HeaderConfig {
  const declared = source.headerConfig ?? {}
  const manualHtml = nonEmpty(declared.manual_html) ?? nonEmpty(source.manualHtml)
  return Object.freeze({
    ...declared,
    ...(nonEmpty(declared.skill_name) || nonEmpty(source.skillName)
      ? { skill_name: nonEmpty(declared.skill_name) ?? nonEmpty(source.skillName) }
      : {}),
    ...(manualHtml ? { manual_html: manualHtml } : {}),
  })
}
