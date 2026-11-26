import { useCallback, useEffect, useState } from 'react'
import { mediaUrl } from './client'
import { useStudioMediaUrl } from './dataContext'

/** Only our immutable browser media endpoint supports derivatives. External
 * URLs and historical fixtures retain their original addressing. */
export function imagePreviewUrl(url: string, edge: 256 | 512 | 1024 = 512, resolve = mediaUrl): string {
  const src = resolve(url)
  const endpoint = /^\/api\/v1\/studio\/(?:v2\/(?:stores\/[a-f0-9]{32}\/)?)?projects\/[^/]+\/media\/[^/?#]+(?:[?#]|$)/
  let owned = endpoint.test(url)
  if (!owned && typeof window !== 'undefined') {
    try {
      const candidate = new URL(src, window.location.origin)
      owned = candidate.origin === new URL(resolve('/'), window.location.origin).origin && endpoint.test(candidate.pathname)
    } catch { return src }
  }
  if (!owned) return src
  const [address, fragment] = src.split('#', 2)
  const [path, search] = address.split('?', 2)
  const query = new URLSearchParams(search)
  query.set('preview', String(edge))
  return `${path}?${query}${fragment ? `#${fragment}` : ''}`
}

export function useStudioImagePreviewUrl() {
  const resolve = useStudioMediaUrl()
  return useCallback((url: string, edge?: 256 | 512 | 1024) => imagePreviewUrl(url, edge, resolve), [resolve])
}

/** Native image lazy loading may fetch every card inside a transformed canvas.
 * Observe the actual clipped viewport before attaching a media URL. */
export function useMediaVisible() {
  const [element, setElement] = useState<HTMLElement | null>(null)
  const ref = useCallback((node: HTMLElement | null) => setElement(node), [])
  const [visible, setVisible] = useState(typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    if (visible || !element || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setVisible(true); observer.disconnect() }
    }, { rootMargin: '120px' })
    observer.observe(element)
    return () => observer.disconnect()
  }, [element, visible])
  return { ref, visible }
}
