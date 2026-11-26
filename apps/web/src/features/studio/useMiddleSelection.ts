import { useCallback, useEffect, useRef, useState, type PointerEvent, type RefObject } from 'react'
import type { ReactFlowInstance } from '@xyflow/react'
import type { StudioNode } from './projection'
import type { Bounds, Point } from './canvasGeometry'
import { nodesInRectangle, rectangleBetween } from './canvasSelection'

type Options = {
  projectId: string
  enabled: boolean
  surface: RefObject<HTMLDivElement>
  flow: RefObject<ReactFlowInstance<StudioNode> | null>
  selected: ReadonlySet<string>
  onSelect: (ids: Set<string>) => void
}
type Gesture = { projectId: string; pointerId: number; start: Point; previous: Set<string>; target: HTMLDivElement }

/** Mouse button 1 is intentionally separate from React Flow's left-button selection. */
export function useMiddleSelection(options: Options) {
  const latest = useRef(options)
  latest.current = options
  const gesture = useRef<Gesture | null>(null)
  const [marquee, setMarquee] = useState<(Bounds & { projectId: string }) | null>(null)
  const release = useCallback(() => {
    const current = gesture.current
    gesture.current = null
    if (current) try { current.target.releasePointerCapture(current.pointerId) } catch { /* Capture may already have been lost. */ }
    setMarquee(null)
  }, [])
  const cancel = useCallback((restore = true) => {
    const current = gesture.current
    if (restore && current?.projectId === latest.current.projectId) latest.current.onSelect(current.previous)
    release()
  }, [release])
  useEffect(() => {
    const blur = () => cancel()
    const visibility = () => { if (document.hidden) cancel() }
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { cancel(false); latest.current.onSelect(new Set()) }
    }
    window.addEventListener('blur', blur)
    window.addEventListener('keydown', key)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      window.removeEventListener('blur', blur)
      window.removeEventListener('keydown', key)
      document.removeEventListener('visibilitychange', visibility)
      const current = gesture.current
      gesture.current = null
      if (current) try { current.target.releasePointerCapture(current.pointerId) } catch { /* Unmounted surface. */ }
    }
  }, [cancel])
  useEffect(() => { cancel(false) }, [options.projectId, options.enabled, cancel])
  const update = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const current = gesture.current
    const { flow, surface, onSelect, projectId } = latest.current
    if (!current || current.pointerId !== event.pointerId || current.projectId !== projectId || !flow.current || !surface.current) return
    const bounds = surface.current.getBoundingClientRect()
    const end = { x: Math.max(bounds.left, Math.min(bounds.right, event.clientX)), y: Math.max(bounds.top, Math.min(bounds.bottom, event.clientY)) }
    const screenRect = rectangleBetween(current.start, end)
    setMarquee({ x: screenRect.x - bounds.left, y: screenRect.y - bounds.top, width: screenRect.width, height: screenRect.height, projectId })
    onSelect(screenRect.width < 3 && screenRect.height < 3 ? new Set()
      : nodesInRectangle(rectangleBetween(flow.current.screenToFlowPosition(current.start), flow.current.screenToFlowPosition(end)), flow.current.getNodes()))
  }, [])
  const onPointerDownCapture = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 1) return
    event.preventDefault(); event.stopPropagation()
    const { enabled, projectId, selected, onSelect } = latest.current
    if (!enabled) return
    gesture.current = { projectId, pointerId: event.pointerId, start: { x: event.clientX, y: event.clientY }, previous: new Set(selected), target: event.currentTarget }
    try { event.currentTarget.setPointerCapture(event.pointerId) } catch { /* Some embedded browsers do not implement pointer capture. */ }
    onSelect(new Set())
    update(event)
  }, [update])
  const onPointerMoveCapture = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (!gesture.current || gesture.current.pointerId !== event.pointerId) return
    event.preventDefault(); event.stopPropagation(); update(event)
  }, [update])
  const onPointerUpCapture = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (!gesture.current || gesture.current.pointerId !== event.pointerId) return
    event.preventDefault(); event.stopPropagation(); update(event); release()
  }, [release, update])
  return { marquee: marquee?.projectId === options.projectId ? marquee : null, cancel,
    handlers: { onPointerDownCapture, onPointerMoveCapture, onPointerUpCapture,
      onPointerCancelCapture: () => cancel(), onLostPointerCapture: () => { if (gesture.current) cancel() } } }
}
