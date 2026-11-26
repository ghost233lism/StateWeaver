import type { Edge } from '@xyflow/react'
import type { Point } from './canvasGeometry'
import { dependencyCrossingGaps, dependencyGeometryKey, routeDependencyGraph, type DependencyBox, type DependencyLink, type DependencyRoute } from './dependencyRouting'
import type { StudioNode } from './projection'
import { STUDIO_CARD_HEIGHT, STUDIO_CARD_WIDTH } from './canvasMetrics'

export type RouteRequest = { key: string; boxes: DependencyBox[]; links: DependencyLink[] }
export type RoutePlan = { key: string; routes: Map<string, DependencyRoute>; gaps: Map<string, Point[][]> }
export function dependencyRequest(nodes: readonly StudioNode[], edges: readonly Edge[]): RouteRequest {
  const boxes = nodes.filter((node) => !node.hidden && !node.parentId && node.type !== 'globalMemory').map((node) => ({
    id: node.id, ...node.position, width: node.width ?? node.measured?.width ?? STUDIO_CARD_WIDTH,
    height: node.height ?? node.measured?.height ?? STUDIO_CARD_HEIGHT, anchorWidth: STUDIO_CARD_WIDTH, anchorHeight: STUDIO_CARD_HEIGHT,
  }))
  const links = edges.filter((edge) => edge.id.startsWith('dependency:')).map(({ id, source, target }) => ({ id, source, target }))
  return { key: dependencyGeometryKey(boxes, links), boxes, links }
}
export function computeDependencyPlan(request: RouteRequest): RoutePlan {
  const routes = routeDependencyGraph(request.boxes, request.links)
  return { key: request.key, routes, gaps: dependencyCrossingGaps(request.links, new Map([...routes].map(([id, route]) => [id, route.points]))) }
}
export function applyDependencyPorts(nodes: StudioNode[], edges: Edge[], routes: Map<string, DependencyRoute>) {
  const ports = new Map<string, NonNullable<StudioNode['data']['ports']>>()
  for (const edge of edges) {
    const route = routes.get(edge.id)
    if (!route) continue
    ports.set(edge.source, [...(ports.get(edge.source) ?? []), { id: edge.sourceHandle!, type: 'source', ...route.source }])
    ports.set(edge.target, [...(ports.get(edge.target) ?? []), { id: edge.targetHandle!, type: 'target', ...route.target }])
  }
  return nodes.map((node) => node.type === 'setGroup' ? { ...node, data: { ...node.data, ports: ports.get(node.id) ?? [] } } : node)
}
