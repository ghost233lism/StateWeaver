export type PreviewTile = { index: number; width: number; height: number }
export type PreviewComposition = { direction: 'row' | 'column'; width: number; height: number; tiles: PreviewTile[] }

const gap = 6
const extraImageCost = 0.07

/** Compare contained single images and small strips. Every tile keeps its aspect
 * ratio; an extra image must improve useful coverage enough to earn its place. */
export function choosePreviewComposition(ratios: readonly number[], width: number, height: number): PreviewComposition | undefined {
  if (!ratios.length || width <= 0 || height <= 0) return undefined
  const valid = ratios.map((ratio) => Number.isFinite(ratio) && ratio > 0 ? ratio : 1)
  let best: PreviewComposition | undefined
  let bestScore = -Infinity
  const consider = (indices: number[], direction: 'row' | 'column') => {
    const horizontal = direction === 'row'
    const spacing = gap * (indices.length - 1)
    const sum = indices.reduce((total, index) => total + (horizontal ? valid[index] : 1 / valid[index]), 0)
    const cross = Math.max(0, Math.min(horizontal ? height : width, ((horizontal ? width : height) - spacing) / sum))
    const tiles = indices.map((index) => ({ index, width: horizontal ? cross * valid[index] : cross,
      height: horizontal ? cross : cross / valid[index] }))
    // Avoid squeezing several complete pictures into unreadable miniatures.
    const minimum = indices.length === 3 ? 88 : 76
    if (indices.length > 1 && tiles.some((tile) => Math.min(tile.width, tile.height) < minimum)) return
    const coverage = tiles.reduce((total, tile) => total + tile.width * tile.height, 0) / (width * height)
    const score = coverage - extraImageCost * (indices.length - 1)
    if (score <= bestScore + 0.001) return
    bestScore = score
    best = { direction, width: horizontal ? cross * sum + spacing : cross,
      height: horizontal ? cross : cross * sum + spacing, tiles }
  }
  // Keep a bounded, aspect-diverse search pool for states with many view variants.
  // Every image competes as a single; equivalent ratios keep their original order.
  valid.forEach((_, index) => consider([index], 'row'))
  const pool: number[] = []
  const buckets = new Map<number, number>()
  valid.map((ratio, index) => ({ ratio, index })).sort((a, b) => a.ratio - b.ratio || a.index - b.index).forEach(({ ratio, index }) => {
    const bucket = Math.round(Math.log2(ratio) * 4)
    if ((buckets.get(bucket) ?? 0) < 3) { pool.push(index); buckets.set(bucket, (buckets.get(bucket) ?? 0) + 1) }
  })
  const search = pool.length <= 24 ? pool : Array.from({ length: 24 }, (_, i) => pool[Math.round(i * (pool.length - 1) / 23)])
  for (let a = 0; a < search.length; a++) {
    for (let b = a + 1; b < search.length; b++) {
      const pair = [search[a], search[b]].sort((x, y) => x - y)
      consider(pair, 'row'); consider(pair, 'column')
      for (let c = b + 1; c < search.length; c++) {
        const triple = [...pair, search[c]].sort((x, y) => x - y)
        consider(triple, 'row'); consider(triple, 'column')
      }
    }
  }
  return best
}
