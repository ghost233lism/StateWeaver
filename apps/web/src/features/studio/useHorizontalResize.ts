import { tr } from "../../ui/locale"
import { useCallback, useEffect, useRef, useState, type HTMLAttributes, type PointerEvent } from 'react'

export function useHorizontalResize({ preferredWidth, minimum, limit, save, measureWidth }: {
  preferredWidth: number
  minimum: number
  limit: number
  save: (width: number | null) => void
  measureWidth?: () => number | undefined
}) {
  const [preview, setPreview] = useState<number | null>(null)
  const gesture = useRef<{ pointerId: number; x: number; width: number; target: HTMLDivElement } | null>(null)
  const clamp = (value: number) => Math.max(minimum, Math.min(limit, Math.round(value)))
  const width = preview ?? clamp(preferredWidth)

  const cancel = useCallback(() => {
    const previous = gesture.current
    gesture.current = null
    setPreview(null)
    if (previous?.target.hasPointerCapture(previous.pointerId)) previous.target.releasePointerCapture(previous.pointerId)
  }, [])

  useEffect(() => {
    window.addEventListener('blur', cancel)
    return () => window.removeEventListener('blur', cancel)
  }, [cancel])

  const cancelPointer = (event: PointerEvent<HTMLDivElement>) => {
    if (gesture.current?.pointerId === event.pointerId) cancel()
  }
  const separator: HTMLAttributes<HTMLDivElement> = {
    role: 'separator', 'aria-orientation': 'vertical', 'aria-valuemin': minimum, 'aria-valuemax': limit,
    'aria-valuenow': width, 'aria-valuetext': tr("{{0}} 像素", width), tabIndex: 0, title: tr("拖动调整宽度；双击恢复默认，方向键微调"),
    onPointerDown(event) {
      if (event.button !== 0 || gesture.current) return
      event.preventDefault(); event.stopPropagation()
      event.currentTarget.focus({ preventScroll: true })
      event.currentTarget.setPointerCapture(event.pointerId)
      gesture.current = { pointerId: event.pointerId, x: event.clientX, width: measureWidth?.() || width, target: event.currentTarget }
    },
    onPointerMove(event) {
      const current = gesture.current
      if (current?.pointerId === event.pointerId) setPreview(clamp(current.width + event.clientX - current.x))
    },
    onPointerUp(event) {
      const current = gesture.current
      if (!current || current.pointerId !== event.pointerId) return
      const next = clamp(current.width + event.clientX - current.x)
      cancel()
      if (next !== Math.round(current.width)) save(next)
    },
    onPointerCancel: cancelPointer,
    onLostPointerCapture: cancelPointer,
    onDoubleClick() { cancel(); save(null) },
    onKeyDown(event) {
      if (event.key === 'Escape' && gesture.current) { event.preventDefault(); event.stopPropagation(); cancel(); return }
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter'].includes(event.key)) return
      event.preventDefault(); event.stopPropagation(); cancel()
      if (event.key === 'Enter') save(null)
      else save(event.key === 'Home' ? minimum : event.key === 'End' ? limit : clamp(width + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 48 : 16)))
    },
  }
  return { width, resizing: preview !== null, cancel, separator }
}
