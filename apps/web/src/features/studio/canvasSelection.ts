import type { StudioNode } from './projection'
import type { WorkspaceLayout } from './model'
import type { Bounds, Point } from './canvasGeometry'

export const rectangleBetween = (a: Point, b: Point): Bounds => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y) })
const intersects = (a: Bounds, b: Bounds) => a.x <= b.x + b.width && a.x + a.width >= b.x && a.y <= b.y + b.height && a.y + a.height >= b.y
const contains = (a: Bounds, b: Bounds) => a.x <= b.x && a.y <= b.y && a.x + a.width >= b.x + b.width && a.y + a.height >= b.y + b.height

export function absoluteNodeBounds(node: StudioNode, nodes: StudioNode[]): Bounds {
  const position = { ...node.position }
  const visited = new Set([node.id])
  let parentId = node.parentId
  while (parentId && !visited.has(parentId)) {
    visited.add(parentId)
    const parent = nodes.find((item) => item.id === parentId)
    if (!parent) break
    position.x += parent.position.x; position.y += parent.position.y
    parentId = parent.parentId
  }
  return { ...position, width: node.width ?? node.measured?.width ?? 292, height: node.height ?? node.measured?.height ?? 220 }
}

/** Moving a selected frame already moves its children through relative coordinates. */
export function topLevelSelection(ids: Iterable<string>, nodes: StudioNode[]): Set<string> {
  const selected = new Set([...ids].map((id) => {
    const node = nodes.find((item) => item.id === id)
    return node?.hidden && node.parentId ? node.parentId : id
  }))
  return new Set(nodes.filter((node) => {
    if (!selected.has(node.id)) return false
    const visited = new Set([node.id])
    let parentId = node.parentId
    while (parentId && !visited.has(parentId)) {
      if (selected.has(parentId)) return false
      visited.add(parentId)
      parentId = nodes.find((parent) => parent.id === parentId)?.parentId
    }
    return true
  }).map((node) => node.id))
}

export function nodesInRectangle(rectangle: Bounds, nodes: StudioNode[]): Set<string> {
  const hits = nodes.filter((node) => {
    if (node.hidden) return false
    const bounds = absoluteNodeBounds(node, nodes)
    return node.type === 'setGroup' && node.data.expanded ? contains(rectangle, bounds) : intersects(rectangle, bounds)
  }).map((node) => node.id)
  return topLevelSelection(hits, nodes)
}

export function movedNodePatch(nodes: StudioNode[], origins: ReadonlyMap<string, Point>, saved: NonNullable<WorkspaceLayout['nodes']>) {
  const movers = topLevelSelection(nodes.map((node) => node.id), nodes)
  return Object.fromEntries(nodes.filter((node) => {
    const origin = origins.get(node.id)
    return movers.has(node.id) && origin && (Math.abs(node.position.x - origin.x) > 0.001 || Math.abs(node.position.y - origin.y) > 0.001)
  }).map((node) => [node.id, { width: node.type === 'globalMemory' ? node.width ?? null : saved[node.id]?.width ?? null,
    height: node.type === 'globalMemory' ? node.data.manualHeight ?? null : saved[node.id]?.height ?? null,
    x: node.parentId ? Math.max(16, node.position.x) : node.position.x, y: node.parentId ? Math.max(64, node.position.y) : node.position.y }]))
}
