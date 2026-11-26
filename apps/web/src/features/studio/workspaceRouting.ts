import type { Edge, Node } from '@xyflow/react'
import type { Bounds } from './canvasGeometry'
import { STUDIO_CARD_WIDTH } from './canvasMetrics'

/** Shared memory floats above the canvas and never redirects production paths. */
export function workspaceEdgeObstacles(nodes: readonly Node[], edge: Pick<Edge, 'id' | 'source'>): Bounds[] {
  const source = nodes.find((node) => node.id === edge.source)
  const outsideGroups = edge.id.startsWith('dependency:') || !source?.parentId
  return nodes.filter((node) => !node.hidden && node.type !== 'globalMemory' && (outsideGroups ? !node.parentId
    // Internal Clip paths stay inside their own frame and avoid its actual cards.
    : node.type !== 'setGroup' && node.parentId === source.parentId)).map((node) => {
    const parent = node.parentId ? nodes.find((candidate) => candidate.id === node.parentId) : undefined
    return { x: node.position.x + (parent?.position.x ?? 0), y: node.position.y + (parent?.position.y ?? 0),
      width: node.width ?? node.measured?.width ?? STUDIO_CARD_WIDTH,
      height: node.height ?? node.measured?.height ?? 220 }
  })
}
