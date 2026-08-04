/**
 * assetActions — 资产本地动作(复制链接 / 复制图片 / 下载),GalleryEditor / AssetEditor 共用。
 *
 * 全是浏览器本地 DOM 行为,**不走 dispatch**(不改 Document)。amber 明文 http 是非安全上下文,
 * navigator.clipboard 不可用 → copyImage 静默兜底为下载(见 feedback_amber_http_breaks_clipboard)。
 */

export function copyText(text: string): void {
  // 非安全上下文(amber 明文 http)下 writeText 会 reject → 静默吞掉(无副作用,UI 不报错)
  navigator.clipboard?.writeText(text).catch(() => { /* non-secure context: no-op */ })
}

export function triggerDownload(url: string, name: string): void {
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/** 复制图片到剪贴板:fetch blob → ClipboardItem;非安全上下文 / 不支持时兜底下载。 */
export async function copyImageToClipboard(url: string, fallbackName: string): Promise<void> {
  try {
    if (!navigator.clipboard || !('write' in navigator.clipboard) || typeof ClipboardItem === 'undefined') {
      triggerDownload(url, fallbackName); return
    }
    const blob = await (await fetch(url)).blob()
    await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })])
  } catch {
    triggerDownload(url, fallbackName)
  }
}
