/**
 * Single trust boundary for operation-manual fragments.
 *
 * Downstream tools own the manual's content, while Build V3 exclusively owns
 * executable behaviour and visual CSS.  Consequently this module accepts only
 * a small semantic/media HTML vocabulary, strips all fragment CSS and active
 * content, resolves media links against the final fragment URL, and is the only
 * component allowed to render the resulting HTML.
 */

import DOMPurify from 'dompurify'
import { createElement, useEffect, useState, type ReactNode } from 'react'

export const DEFAULT_OPERATION_MANUAL_SOURCE = '/operation-manual.html'

const ALLOWED_TAGS = [
  'section', 'article', 'header', 'footer', 'main', 'nav', 'aside',
  'div', 'span', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'blockquote', 'pre', 'code',
  'kbd', 'samp', 'strong', 'em', 'b', 'i', 'u', 'mark', 'small',
  'sub', 'sup', 'br', 'hr', 'a', 'figure', 'figcaption',
  'img', 'picture', 'video', 'audio', 'source', 'track',
  'table', 'caption', 'colgroup', 'col', 'thead', 'tbody', 'tfoot',
  'tr', 'th', 'td', 'details', 'summary', 'time', 'abbr',
]

const ALLOWED_ATTRIBUTES = [
  'class', 'role', 'title',
  'href', 'target', 'rel',
  'src', 'srcset', 'sizes', 'alt', 'width', 'height', 'loading', 'decoding',
  'controls', 'poster', 'preload', 'muted', 'loop', 'playsinline',
  'type', 'media', 'kind', 'srclang', 'label', 'default',
  'scope', 'colspan', 'rowspan', 'open', 'datetime',
  'aria-label', 'aria-describedby', 'aria-hidden',
]

const FORBIDDEN_TAGS = [
  'script', 'style', 'link', 'iframe', 'frame', 'frameset', 'object',
  'embed', 'form', 'input', 'button', 'select', 'option', 'textarea',
  'svg', 'math', 'canvas', 'template', 'base', 'meta', 'noscript',
]

const SAFE_MANUAL_CLASS = /^ze-om[A-Za-z0-9_-]*$/
const SAFE_LINK_PROTOCOLS = new Set(['http:', 'https:', 'mailto:', 'tel:'])
const SAFE_MEDIA_PROTOCOLS = new Set(['http:', 'https:'])
const ELEMENT_NODE = 1
const TEXT_NODE = 3
const manualBrand: unique symbol = Symbol('SanitizedOperationManualFragment')

export type SanitizedOperationManualFragment = Readonly<{
  html: string
  sourceUrl: string
  [manualBrand]: true
}>

export type OperationManualLoadState = Readonly<{
  status: 'loading' | 'ready' | 'unavailable'
  fragment: SanitizedOperationManualFragment | null
}>

type OperationManualLoadOptions = {
  baseUrl?: string
  fetcher?: typeof fetch
  signal?: AbortSignal
}

function operationManualSource(source?: string): string {
  return source?.trim() || DEFAULT_OPERATION_MANUAL_SOURCE
}

/** Resolve only fetchable HTTP(S) fragment locations. */
export function resolveOperationManualUrl(source?: string, baseUrl?: string): URL | null {
  const base = baseUrl
    || globalThis.document?.baseURI
    || globalThis.location?.href
  if (!base) return null
  try {
    const resolved = new URL(operationManualSource(source), base)
    return SAFE_MEDIA_PROTOCOLS.has(resolved.protocol) ? resolved : null
  } catch {
    return null
  }
}

function safeAbsoluteUrl(
  raw: string,
  sourceUrl: URL,
  protocols: ReadonlySet<string>,
): string | null {
  try {
    const resolved = new URL(raw, sourceUrl)
    if (!protocols.has(resolved.protocol)) return null
    if ((resolved.protocol === 'http:' || resolved.protocol === 'https:')
        && (resolved.username || resolved.password)) return null
    return resolved.href
  } catch {
    return null
  }
}

function safeSrcset(raw: string, sourceUrl: URL): string | null {
  const candidates = raw.split(',').map((item) => item.trim()).filter(Boolean)
  if (candidates.length === 0) return null
  const normalized: string[] = []
  for (const candidate of candidates) {
    const match = candidate.match(/^(\S+?)(?:\s+([0-9]+w|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)x))?$/)
    if (!match) return null
    const url = safeAbsoluteUrl(match[1], sourceUrl, SAFE_MEDIA_PROTOCOLS)
    if (!url) return null
    normalized.push(match[2] ? `${url} ${match[2]}` : url)
  }
  return normalized.join(', ')
}

function normalizeClasses(element: Element): void {
  const classes = (element.getAttribute('class') || '')
    .split(/\s+/)
    .filter((token) => SAFE_MANUAL_CLASS.test(token))
  if (classes.length > 0) element.setAttribute('class', [...new Set(classes)].join(' '))
  else element.removeAttribute('class')
}

function normalizeUrlAttribute(
  element: Element,
  name: string,
  sourceUrl: URL,
  protocols: ReadonlySet<string>,
): void {
  const raw = element.getAttribute(name)
  if (raw === null) return
  const safe = safeAbsoluteUrl(raw, sourceUrl, protocols)
  if (safe) element.setAttribute(name, safe)
  else element.removeAttribute(name)
}

function normalizeElement(element: Element, sourceUrl: URL): void {
  normalizeClasses(element)
  const tag = element.tagName.toLowerCase()

  if (tag === 'a') {
    normalizeUrlAttribute(element, 'href', sourceUrl, SAFE_LINK_PROTOCOLS)
    if (!element.hasAttribute('href')) {
      element.removeAttribute('target')
      element.removeAttribute('rel')
      return
    }
    const target = element.getAttribute('target')
    if (target !== '_blank' && target !== '_self') element.removeAttribute('target')
    if (element.getAttribute('target') === '_blank' && element.hasAttribute('href')) {
      element.setAttribute('rel', 'noopener noreferrer')
    } else {
      element.removeAttribute('rel')
    }
  }

  if (tag === 'img' || tag === 'video' || tag === 'audio' || tag === 'source'
      || tag === 'track') {
    normalizeUrlAttribute(element, 'src', sourceUrl, SAFE_MEDIA_PROTOCOLS)
  }
  if (tag === 'video') {
    normalizeUrlAttribute(element, 'poster', sourceUrl, SAFE_MEDIA_PROTOCOLS)
  }
  if (tag === 'img' || tag === 'source') {
    const srcset = element.getAttribute('srcset')
    if (srcset !== null) {
      const safe = safeSrcset(srcset, sourceUrl)
      if (safe) element.setAttribute('srcset', safe)
      else element.removeAttribute('srcset')
    }
  }
}

/**
 * Sanitize one standard `<section class="ze-om">…</section>` fragment.
 * A single marked root is mandatory; siblings are rejected instead of being
 * silently rendered outside the manual's styling and ownership boundary.
 */
export function sanitizeOperationManualFragment(
  rawHtml: string,
  source: string | URL,
): SanitizedOperationManualFragment | null {
  if (!rawHtml || typeof DOMPurify.sanitize !== 'function') return null
  const sourceUrl = source instanceof URL
    ? resolveOperationManualUrl(source.href, source.href)
    : resolveOperationManualUrl(source, globalThis.document?.baseURI || source)
  if (!sourceUrl) return null

  const fragment: DocumentFragment = DOMPurify.sanitize(rawHtml, {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ALLOWED_ATTRIBUTES,
    ALLOW_ARIA_ATTR: true,
    ALLOW_DATA_ATTR: false,
    ALLOW_UNKNOWN_PROTOCOLS: false,
    FORBID_TAGS: FORBIDDEN_TAGS,
    FORBID_ATTR: ['style', 'srcdoc'],
    RETURN_DOM_FRAGMENT: true,
    SANITIZE_DOM: true,
    SANITIZE_NAMED_PROPS: true,
  })

  const meaningfulNodes = Array.from(fragment.childNodes).filter((node) => (
    node.nodeType === ELEMENT_NODE
    || (node.nodeType === TEXT_NODE && !!node.textContent?.trim())
  ))
  if (meaningfulNodes.length !== 1 || meaningfulNodes[0].nodeType !== ELEMENT_NODE) return null
  const root = meaningfulNodes[0] as Element

  const elements = [root, ...Array.from(root.querySelectorAll('*'))]
  for (const element of elements) normalizeElement(element, sourceUrl)
  if (!root.classList.contains('ze-om')) return null

  return Object.freeze({
    html: root.outerHTML,
    sourceUrl: sourceUrl.href,
    [manualBrand]: true as const,
  })
}

/** Fetch, redirect-resolve and sanitize one operation-manual fragment. */
export async function loadOperationManualFragment(
  source?: string,
  options: OperationManualLoadOptions = {},
): Promise<SanitizedOperationManualFragment | null> {
  const requestedUrl = resolveOperationManualUrl(source, options.baseUrl)
  if (!requestedUrl) return null
  const fetcher = options.fetcher ?? globalThis.fetch
  if (!fetcher) return null
  const response = await fetcher(requestedUrl.href, {
    method: 'GET',
    credentials: 'same-origin',
    cache: 'no-cache',
    signal: options.signal,
  })
  if (!response.ok) return null
  const finalUrl = resolveOperationManualUrl(response.url || requestedUrl.href, requestedUrl.href)
  if (!finalUrl) return null
  return sanitizeOperationManualFragment(await response.text(), finalUrl)
}

/** Latest-only React loader shared by every manual surface. */
export function useOperationManual(source?: string): OperationManualLoadState {
  const sourceKey = operationManualSource(source)
  const [state, setState] = useState<OperationManualLoadState & { sourceKey: string }>({
    sourceKey,
    status: 'loading',
    fragment: null,
  })

  useEffect(() => {
    const controller = new AbortController()
    let alive = true
    setState({ sourceKey, status: 'loading', fragment: null })
    void loadOperationManualFragment(sourceKey, { signal: controller.signal })
      .then((fragment) => {
        if (!alive) return
        setState({
          sourceKey,
          status: fragment ? 'ready' : 'unavailable',
          fragment,
        })
      })
      .catch(() => {
        if (alive) setState({ sourceKey, status: 'unavailable', fragment: null })
      })
    return () => {
      alive = false
      controller.abort()
    }
  }, [sourceKey])

  if (state.sourceKey !== sourceKey) return { status: 'loading', fragment: null }
  return { status: state.status, fragment: state.fragment }
}

/** The sole renderer for branded, sanitized manual HTML. */
export function OperationManualContent({
  fragment,
  className,
}: {
  fragment: SanitizedOperationManualFragment
  className?: string
}): ReactNode {
  // Type branding protects TypeScript callers; the private runtime Symbol also
  // fails closed for JS, `any`, stale bundles and forged deserialized objects.
  if (!fragment || fragment[manualBrand] !== true) return null
  const classes = ['ze-operation-manual', className].filter(Boolean).join(' ')
  return createElement('div', {
    className: classes,
    dangerouslySetInnerHTML: { __html: fragment.html },
  })
}
