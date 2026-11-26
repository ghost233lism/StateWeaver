import { tr } from '../../ui/locale'
import { useCallback, useEffect, useId, useRef, useState, type PointerEvent } from 'react'
import { GripVertical } from 'lucide-react'

// A nested presentation region in the existing project layout, not a canvas node.
export const GLOBAL_STORY_LAYOUT_ID = 'memory:global:story'
const minimum = 220
const visualMinimum = 320
const regionGap = 24
const bounded = (width: number, maximum: number) => Math.max(minimum, Math.min(maximum, Math.round(width)))
type Gesture = { pointerId: number; x: number; width: number; scale: number; maximum: number }

export function useMemoryStoryResize(savedWidth?: number | null, onChange?: (width: number | null) => void) {
  const regions = useRef<HTMLDivElement>(null)
  const story = useRef<HTMLElement>(null)
  const handle = useRef<HTMLDivElement>(null)
  const gesture = useRef<Gesture | null>(null)
  const [preview, setPreview] = useState<number | null>(null)
  const [size, setSize] = useState({ width: minimum, maximum: minimum })
  const storyId = useId()
  const enabled = Boolean(onChange)
  const cancel = useCallback(() => {
    const current = gesture.current
    gesture.current = null
    setPreview(null)
    if (current && handle.current?.hasPointerCapture(current.pointerId)) handle.current.releasePointerCapture(current.pointerId)
  }, [])

  useEffect(() => {
    if (!enabled) { cancel(); return }
    const container = regions.current, section = story.current
    if (!container || !section) return
    const measure = () => {
      const next = { width: section.offsetWidth, maximum: Math.max(minimum, container.clientWidth - visualMinimum - regionGap) }
      setSize((current) => current.width === next.width && current.maximum === next.maximum ? current : next)
    }
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(container)
    observer?.observe(section)
    window.addEventListener('blur', cancel)
    return () => { observer?.disconnect(); window.removeEventListener('blur', cancel); gesture.current = null }
  }, [enabled, cancel])

  const pointerWidth = (event: PointerEvent<HTMLDivElement>, current: Gesture) => bounded(current.width + (event.clientX - current.x) / current.scale, current.maximum)
  const reset = () => { cancel(); if (savedWidth != null) onChange?.(null) }
  const control = enabled && <div ref={handle} className={`global-memory-story-resize nodrag nopan nowheel${preview != null ? ' is-resizing' : ''}`}
    role="separator" aria-label={tr("调整剧情记忆宽度")} aria-orientation="vertical" aria-controls={storyId}
    aria-valuemin={minimum} aria-valuemax={size.maximum} aria-valuenow={size.width} tabIndex={0}
    title={tr("左右拖动调整剧情区宽度，双击恢复自动宽度；方向键微调，Shift 加速")}
    onPointerDown={(event) => {
      if (event.button !== 0 || gesture.current || !regions.current || !story.current) return
      event.preventDefault(); event.stopPropagation()
      const container = regions.current
      const scale = container.getBoundingClientRect().width / container.offsetWidth
      if (!Number.isFinite(scale) || scale <= 0) return
      gesture.current = { pointerId: event.pointerId, x: event.clientX, width: story.current.offsetWidth, scale,
        maximum: Math.max(minimum, container.clientWidth - visualMinimum - regionGap) }
      event.currentTarget.focus({ preventScroll: true })
      event.currentTarget.setPointerCapture(event.pointerId)
    }}
    onPointerMove={(event) => {
      const current = gesture.current
      if (!current || event.pointerId !== current.pointerId) return
      event.preventDefault(); event.stopPropagation()
      setPreview(pointerWidth(event, current))
    }}
    onPointerUp={(event) => {
      const current = gesture.current
      if (!current || event.pointerId !== current.pointerId) return
      event.preventDefault(); event.stopPropagation()
      const next = pointerWidth(event, current)
      cancel()
      if (next !== current.width) onChange?.(next)
    }}
    onPointerCancel={cancel} onLostPointerCapture={cancel} onDoubleClick={(event) => { event.preventDefault(); event.stopPropagation(); reset() }}
    onKeyDown={(event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', 'Escape'].includes(event.key)) return
      event.preventDefault(); event.stopPropagation()
      if (event.key === 'Escape') { cancel(); return }
      if (event.key === 'Enter') { reset(); return }
      const width = story.current?.offsetWidth ?? size.width
      const maximum = Math.max(minimum, (regions.current?.clientWidth ?? 0) - visualMinimum - regionGap)
      const step = event.shiftKey ? 80 : 20
      const next = bounded(event.key === 'Home' ? minimum : event.key === 'End' ? maximum : width + (event.key === 'ArrowLeft' ? -step : step), maximum)
      cancel()
      if (next !== width) onChange?.(next)
    }}><span aria-hidden="true"><GripVertical size={14} /></span></div>

  return { regions, story, storyId, width: preview ?? savedWidth, control }
}
