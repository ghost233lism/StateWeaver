import type { StudioNode } from './projection'

function sameRecord(a: object | undefined, b: object | undefined): boolean {
  if (a === b) return true
  if (!a || !b) return false
  const keys = Object.keys(a) as (keyof typeof a)[]
  return keys.length === Object.keys(b).length && keys.every((key) => Object.is(a[key], b[key]))
}

/** Preserve unchanged content while geometry moves. Compare only the small
 * projection fields; the actual project/version references remain authoritative. */
export function stableCanvasNodes(previous: readonly StudioNode[], incoming: StudioNode[]): StudioNode[] {
  const byId = new Map(previous.map((node) => [node.id, node]))
  return incoming.map((node) => {
    const old = byId.get(node.id)
    if (!old) return node
    const data = { ...node.data }
    if (sameRecord(old.data.selection, data.selection)) data.selection = old.data.selection
    if (old.data.ports?.length === data.ports?.length && data.ports?.every((port, index) => sameRecord(old.data.ports?.[index], port))) data.ports = old.data.ports
    const result = { ...node,
      position: sameRecord(old.position, node.position) ? old.position : node.position,
      data: sameRecord(old.data, data) ? old.data : data,
    }
    if ('measured' in node && sameRecord(old.measured, node.measured)) result.measured = old.measured
    if ('style' in node && sameRecord(old.style, node.style)) result.style = old.style
    return sameRecord(old, result) ? old : result
  })
}
