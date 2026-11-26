import { useEffect, useMemo, useRef, useState } from 'react'
import type { Edge } from '@xyflow/react'
import type { StudioNode } from './projection'
import { computeDependencyPlan, dependencyRequest, type RoutePlan, type RouteRequest } from './dependencyRoutePlan'

/** Coalesce drag/resize bursts and reject old geometry. Status updates never
 * trigger routing. The browser's UI thread only applies the finished plan. */
export function useDependencyRouting(nodes: StudioNode[], edges: Edge[]) {
  const request = dependencyRequest(nodes, edges)
  const wanted = useRef<RouteRequest>(request)
  wanted.current = request
  const worker = useRef<Worker>()
  const busy = useRef(false)
  const sent = useRef('')
  const [unavailable, setUnavailable] = useState(typeof Worker === 'undefined')
  const [plan, setPlan] = useState<RoutePlan>()
  useEffect(() => {
    if (unavailable) return
    let channel: Worker
    try { channel = new Worker(new URL('./dependency-route.worker.ts', import.meta.url), { type: 'module' }) }
    catch { setUnavailable(true); return }
    worker.current = channel
    const send = () => { const value = wanted.current; busy.current = true; sent.current = value.key; channel.postMessage(value) }
    channel.onmessage = (event: MessageEvent<RoutePlan>) => {
      busy.current = false
      if (event.data.key === wanted.current.key) setPlan(event.data)
      else send()
    }
    channel.onerror = () => { channel.terminate(); worker.current = undefined; busy.current = false; setUnavailable(true) }
    send()
    return () => { channel.terminate(); worker.current = undefined; busy.current = false }
  }, [unavailable])
  useEffect(() => {
    if (worker.current && !busy.current && sent.current !== request.key) {
      busy.current = true; sent.current = request.key; worker.current.postMessage(wanted.current)
    }
  }, [request.key])
  const fallback = useMemo(() => unavailable ? computeDependencyPlan(wanted.current) : undefined, [unavailable, request.key])
  return useMemo(() => fallback ?? (plan?.key === request.key ? plan : { key: request.key, routes: new Map(), gaps: new Map() }), [fallback, plan, request.key])
}
