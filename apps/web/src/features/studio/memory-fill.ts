import { useLayoutEffect, useState, type RefObject } from 'react'

const minimumCardHeight = 320
const maximumCardHeight = 420
const gridGap = 16

type Category = { id: string; rows: number; chrome: number; scrollbar: number }
type CategoryLayout = { cardHeight: number; heights: Record<string, number> }

/** Keep one complete card row available per category, then share remaining space
 * among categories that need more rows. Large catalogs scroll in their category. */
export function allocateMemoryCategories(categories: readonly Category[], available?: number, fallback = 656): CategoryLayout {
  if (!categories.length) return { cardHeight: minimumCardHeight, heights: {} }
  const rows = categories.reduce((sum, category) => sum + category.rows, 0)
  const fixed = categories.reduce((sum, category) => sum + category.chrome + category.scrollbar + Math.max(0, category.rows - 1) * gridGap, 0)
  const cardHeight = available != null && rows
    ? Math.max(minimumCardHeight, Math.min(maximumCardHeight, Math.floor((available - fixed) / rows))) : minimumCardHeight
  const full = categories.map((category) => category.rows * cardHeight + Math.max(0, category.rows - 1) * gridGap + category.scrollbar)
  if (available == null) return { cardHeight, heights: Object.fromEntries(categories.map((category, i) =>
    [category.id, Math.min(full[i], Math.max(cardHeight + category.scrollbar, fallback))])) }
  const heights = categories.map((category, i) => Math.min(cardHeight + category.scrollbar, full[i]))
  let remaining = Math.floor(available - categories.reduce((sum, category) => sum + category.chrome, 0) - heights.reduce((sum, height) => sum + height, 0))
  while (remaining > 0) {
    const waiting = heights.map((_, i) => i).filter((i) => heights[i] < full[i])
    if (!waiting.length) break
    const share = Math.max(1, Math.floor(remaining / waiting.length))
    for (const i of waiting) {
      const extra = Math.min(share, remaining, full[i] - heights[i])
      heights[i] += extra
      remaining -= extra
    }
  }
  return { cardHeight, heights: Object.fromEntries(categories.map((category, i) => [category.id, heights[i]])) }
}

export function useMemoryCategories(hostRef: RefObject<HTMLElement>, bounded: boolean, contentKey: string) {
  const [layout, setLayout] = useState<CategoryLayout>({ cardHeight: minimumCardHeight, heights: {} })
  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host) return
    const measure = () => {
      const mobile = window.matchMedia?.('(max-width: 600px)').matches ?? false
      const groups = [...host.querySelectorAll<HTMLElement>('.global-memory-asset-group')]
      const groupGap = Number.parseFloat(getComputedStyle(host).rowGap) || 0
      const categories = groups.flatMap((group) => {
        const viewport = group.querySelector<HTMLElement>('.global-memory-category-viewport')
        const grid = group.querySelector<HTMLElement>('.global-memory-asset-grid')
        if (!viewport || !grid) return []
        const columns = Math.max(1, Math.floor((grid.clientWidth + gridGap) / (320 + gridGap)))
        const count = grid.querySelectorAll('.global-memory-asset-card').length
        return [{ id: group.dataset.assetKind!, rows: Math.ceil(count / columns),
          chrome: Math.max(0, group.offsetHeight - viewport.offsetHeight),
          scrollbar: Math.max(0, viewport.offsetHeight - viewport.clientHeight) }]
      })
      // Only a bounded host provides a height budget. Natural/mobile content
      // never reads its own growing height back into the allocation.
      const available = bounded && !mobile && host.clientHeight > 0
        ? Math.max(0, host.clientHeight - Math.max(0, groups.length - 1) * groupGap) : undefined
      const fallback = mobile ? Math.max(320, Math.min(656, window.innerHeight * 0.6)) : 656
      const next = allocateMemoryCategories(categories, available, fallback)
      setLayout((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next)
    }
    measure()
    window.addEventListener('resize', measure)
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure)
    observer?.observe(host)
    // Observe fixed headings/controls, never the output heights of cards or grids.
    host.querySelectorAll('.global-memory-asset-group > h4, .memory-group-toggle').forEach((element) => observer?.observe(element))
    return () => { observer?.disconnect(); window.removeEventListener('resize', measure) }
  }, [hostRef, bounded, contentKey])
  return layout
}
