import { useCallback, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'

export type TreeCamera = { x: number; y: number; zoom: number }
type Point = { x: number; y: number }
const boundZoom = (value: number) => Math.max(.2, Math.min(2, value))

export function zoomTreeAt(camera: TreeCamera, zoom: number, anchor: Point): TreeCamera {
  const next = boundZoom(zoom)
  return { x: anchor.x - (anchor.x - camera.x) * next / camera.zoom,
    y: anchor.y - (anchor.y - camera.y) * next / camera.zoom, zoom: next }
}

export function centeredTreeCamera(width: number, height: number, graphWidth: number, graphHeight: number, minimum = .2): TreeCamera {
  const zoom = Math.max(minimum, Math.min(1, width / graphWidth, height / graphHeight))
  return { x: (width - graphWidth * zoom) / 2, y: (height - graphHeight * zoom) / 2, zoom }
}

/** Wheel events expose no device ID. Discrete/line wheels zoom; fine pixel
 * scrolling pans. Keep a trackpad gesture through its faster momentum events. */
export function treeWheelAction(event: Pick<WheelEvent, 'ctrlKey' | 'metaKey' | 'deltaMode' | 'deltaX' | 'deltaY'>, ongoingPan = false) {
  if (event.ctrlKey || event.metaKey) return 'zoom'
  if (event.deltaMode === 0 && (ongoingPan || event.deltaX !== 0 || Math.abs(event.deltaY) < 50)) return 'pan'
  return 'zoom'
}

/** Camera motion only changes a compositor transform, never every image node
 * or its detail/history session. There are no scroll bounds or layout writes. */
export function useStateTreeViewport(graph: { width: number; height: number }, geometryKey: string) {
  const viewport = useRef<HTMLDivElement>(null)
  const scene = useRef<HTMLDivElement>(null)
  const camera = useRef<TreeCamera>({ x: 0, y: 0, zoom: 1 })
  const frame = useRef<number>()
  const [zoom, setZoom] = useState(1)
  const size = useRef({ width: 0, height: 0 })
  const fitted = useRef('')
  const dimensions = useRef(graph)
  dimensions.current = graph
  const paint = useCallback(() => {
    frame.current = undefined
    const value = camera.current
    if (scene.current) scene.current.style.transform = `translate3d(${value.x}px, ${value.y}px, 0) scale(${value.zoom})`
    setZoom((previous) => previous === value.zoom ? previous : value.zoom)
  }, [])
  const move = useCallback((value: TreeCamera, immediately = false) => {
    camera.current = value
    if (immediately) {
      if (frame.current !== undefined) cancelAnimationFrame(frame.current)
      paint()
    } else if (frame.current === undefined) frame.current = requestAnimationFrame(paint)
  }, [paint])
  const fit = useCallback((minimum = .2) => {
    const element = viewport.current, bounds = dimensions.current
    if (element?.clientWidth && element.clientHeight) move(centeredTreeCamera(element.clientWidth, element.clientHeight, bounds.width, bounds.height, minimum), true)
  }, [move])
  const zoomBy = useCallback((factor: number) => {
    const element = viewport.current
    move(zoomTreeAt(camera.current, camera.current.zoom * factor, { x: (element?.clientWidth ?? 0) / 2, y: (element?.clientHeight ?? 0) / 2 }), true)
  }, [move])

  useLayoutEffect(() => {
    const element = viewport.current
    if (!element) return
    const measure = () => {
      const width = element.clientWidth, height = element.clientHeight
      if (!width || !height) return
      if (geometryKey && fitted.current !== geometryKey) { fit(.7); fitted.current = geometryKey }
      else if (size.current.width && size.current.height) move({ ...camera.current,
        x: camera.current.x + (width - size.current.width) / 2,
        y: camera.current.y + (height - size.current.height) / 2 }, true)
      size.current = { width, height }
    }
    measure()
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : undefined
    observer?.observe(element)
    return () => observer?.disconnect()
  }, [geometryKey, fit, move])

  useLayoutEffect(() => {
    const element = viewport.current
    if (!element) return
    let lastPan = -Infinity
    const wheel = (event: WheelEvent) => {
      event.preventDefault(); event.stopPropagation()
      const action = treeWheelAction(event, event.timeStamp - lastPan < 160)
      if (action === 'pan') {
        lastPan = event.timeStamp
        move({ ...camera.current, x: camera.current.x - event.deltaX, y: camera.current.y - event.deltaY })
      } else {
        lastPan = -Infinity
        const rect = element.getBoundingClientRect()
        const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientHeight : 1)
        move(zoomTreeAt(camera.current, camera.current.zoom * Math.exp(-Math.max(-200, Math.min(200, delta)) * .002), { x: event.clientX - rect.left, y: event.clientY - rect.top }))
      }
    }
    // React's delegated wheel handler is passive in Chromium.
    element.addEventListener('wheel', wheel, { passive: false })
    return () => {
      element.removeEventListener('wheel', wheel)
      if (frame.current !== undefined) cancelAnimationFrame(frame.current)
      frame.current = undefined
    }
  }, [move])

  const pointers = useRef(new Map<number, Point>())
  const drag = useRef<{ start: Point; camera: TreeCamera }>()
  const pinch = useRef<{ center: Point; distance: number; camera: TreeCamera }>()
  const suppressClick = useRef(false)
  const localPoint = (event: PointerEvent<HTMLDivElement>): Point => {
    const rect = event.currentTarget.getBoundingClientRect()
    return { x: event.clientX - rect.left, y: event.clientY - rect.top }
  }
  const pinchPoints = () => {
    const [a, b] = [...pointers.current.values()]
    return { center: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, distance: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)) }
  }
  const release = (event: PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId)
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    pinch.current = undefined
    const remaining = [...pointers.current.values()][0]
    drag.current = remaining ? { start: remaining, camera: camera.current } : undefined
    if (!remaining) delete event.currentTarget.dataset.panning
  }
  const handlers = {
    onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0 && event.button !== 1) return
      event.stopPropagation()
      if (!pointers.current.size) suppressClick.current = false
      const point = localPoint(event)
      pointers.current.set(event.pointerId, point)
      if (pointers.current.size === 1) drag.current = { start: point, camera: camera.current }
      if (pointers.current.size === 2) { pinch.current = { ...pinchPoints(), camera: camera.current }; suppressClick.current = true }
    },
    onPointerMove: (event: PointerEvent<HTMLDivElement>) => {
      if (!pointers.current.has(event.pointerId)) return
      const point = localPoint(event)
      pointers.current.set(event.pointerId, point)
      if (pinch.current && pointers.current.size >= 2) {
        const current = pinchPoints(), start = pinch.current
        const scaled = zoomTreeAt(start.camera, start.camera.zoom * current.distance / start.distance, start.center)
        move({ ...scaled, x: scaled.x + current.center.x - start.center.x, y: scaled.y + current.center.y - start.center.y })
      } else if (drag.current) {
        const start = drag.current, dx = point.x - start.start.x, dy = point.y - start.start.y
        if (!suppressClick.current && Math.abs(dx) + Math.abs(dy) <= 4) return
        suppressClick.current = true
        move({ ...start.camera, x: start.camera.x + dx, y: start.camera.y + dy })
      }
      event.currentTarget.setPointerCapture?.(event.pointerId)
      event.currentTarget.dataset.panning = 'true'
      event.stopPropagation()
    },
    onPointerUp: release,
    onPointerCancel: release,
    onLostPointerCapture: release,
    onClickCapture: (event: React.MouseEvent<HTMLDivElement>) => {
      if (suppressClick.current) { event.preventDefault(); event.stopPropagation() }
      suppressClick.current = false
    },
    onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.target !== event.currentTarget) return
      const offsets: Record<string, Point> = { ArrowLeft: { x: 48, y: 0 }, ArrowRight: { x: -48, y: 0 }, ArrowUp: { x: 0, y: 48 }, ArrowDown: { x: 0, y: -48 } }
      const offset = offsets[event.key]
      if (offset) move({ ...camera.current, x: camera.current.x + offset.x, y: camera.current.y + offset.y })
      else if (event.key === '+' || event.key === '=') zoomBy(1.25)
      else if (event.key === '-') zoomBy(1 / 1.25)
      else if (event.key === 'Home') fit()
      else return
      event.preventDefault(); event.stopPropagation()
    },
  }
  return { viewport, scene, zoom, zoomBy, fit, handlers }
}
