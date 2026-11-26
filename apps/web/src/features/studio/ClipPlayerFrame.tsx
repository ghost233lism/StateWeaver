import { tr, useLocale } from '../../ui/locale'
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, GripHorizontal } from 'lucide-react'

export const CLIP_PLAYER_HEIGHT_KEY = 'studio:clip-player-height'
const defaultHeight = 380
const minimumHeight = 176
const collapsedThreshold = 72
const bounded = (height: number, limit: number) => height <= collapsedThreshold ? 0 : Math.max(minimumHeight, Math.min(limit, Math.round(height)))
function storedHeight() {
  try {
    const saved = localStorage.getItem(CLIP_PLAYER_HEIGHT_KEY)
    const value = saved === null ? null : Number(saved)
    return value !== null && Number.isFinite(value) && value >= 0 ? value : null
  } catch { return null }
}

/** A local viewing preference; resizing never changes project layout or remounts the video. */
export function ClipPlayerFrame({ children, onCollapse }: {
  children: (controls: { collapse: () => void }) => ReactNode; onCollapse: () => void
}) {
  useLocale()

  const [preferred, setPreferred] = useState(storedHeight)
  const [preview, setPreview] = useState<number | null>(null)
  const [limit, setLimit] = useState(Math.max(minimumHeight, window.innerHeight - 360))
  const frame = useRef<HTMLDivElement>(null)
  const handle = useRef<HTMLDivElement>(null)
  const gesture = useRef<{ pointerId: number; y: number; height: number } | null>(null)
  const rememberedHeight = useRef(preferred || defaultHeight)
  const collapseCallback = useRef(onCollapse)
  collapseCallback.current = onCollapse
  const height = bounded(preview ?? preferred ?? defaultHeight, limit)
  const collapsed = height === 0
  const id = useId()
  const cancel = useCallback(() => {
    const current = gesture.current
    gesture.current = null
    setPreview(null)
    if (current && handle.current?.hasPointerCapture(current.pointerId)) handle.current.releasePointerCapture(current.pointerId)
  }, [])
  const save = (value: number | null) => {
    setPreferred(value)
    if (value) rememberedHeight.current = value
    try {
      if (value === null) localStorage.removeItem(CLIP_PLAYER_HEIGHT_KEY)
      else localStorage.setItem(CLIP_PLAYER_HEIGHT_KEY, String(value))
    } catch { /* Viewing remains usable without browser storage. */ }
  }
  const toggle = () => { cancel(); save(collapsed ? bounded(rememberedHeight.current, limit) : 0) }
  useLayoutEffect(() => {
    const parent = frame.current?.parentElement
    if (!parent) return
    const measure = () => {
      const available = parent.getBoundingClientRect().height
      if (available > 0) setLimit(Math.max(minimumHeight, Math.floor(available - 190)))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(parent)
    window.addEventListener('resize', measure)
    return () => { observer.disconnect(); window.removeEventListener('resize', measure) }
  }, [])
  useEffect(() => { if (collapsed) collapseCallback.current() }, [collapsed])
  useEffect(() => { window.addEventListener('blur', cancel); return () => window.removeEventListener('blur', cancel) }, [cancel])
  return <>
    <div ref={frame} id={`${id}-frame`} className="studio-clip-player-frame" style={{ height }} hidden={collapsed}>
      {children({ collapse: () => { cancel(); save(0) } })}
    </div>
    <div ref={handle} className={`studio-clip-player-resize${collapsed ? ' is-collapsed' : ''}`} role="separator" tabIndex={0}
      aria-label={tr("调整播放器高度")} aria-orientation="horizontal" aria-controls={`${id}-frame`} aria-valuemin={0} aria-valuemax={limit} aria-valuenow={height} aria-valuetext={collapsed ? tr("播放器已隐藏，向下拖动展开") : tr("播放器高度 {{0}} 像素", height)}
      title={collapsed ? tr("向下拖动或点击展开播放器") : tr("上下拖动调整播放器，拖到顶部隐藏；双击收起")}
      onPointerDown={(event) => {
        if (event.button !== 0 || (event.target as HTMLElement).closest('button')) return
        event.preventDefault(); event.stopPropagation()
        gesture.current = { pointerId: event.pointerId, y: event.clientY, height }
        event.currentTarget.setPointerCapture(event.pointerId)
      }}
      onPointerMove={(event) => {
        const current = gesture.current
        if (current?.pointerId === event.pointerId) setPreview(bounded(current.height + event.clientY - current.y, limit))
      }}
      onPointerUp={(event) => {
        const current = gesture.current
        if (!current || current.pointerId !== event.pointerId) return
        const next = bounded(current.height + event.clientY - current.y, limit)
        cancel()
        if (next !== current.height) save(next)
        else if (collapsed) save(bounded(rememberedHeight.current, limit))
      }}
      onPointerCancel={(event) => { if (gesture.current?.pointerId === event.pointerId) cancel() }}
      onLostPointerCapture={(event) => { if (gesture.current?.pointerId === event.pointerId) cancel() }}
      onDoubleClick={toggle}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && gesture.current) { event.preventDefault(); event.stopPropagation(); cancel(); return }
        if (!['ArrowUp', 'ArrowDown', 'Home', 'End', 'Enter'].includes(event.key)) return
        event.preventDefault(); event.stopPropagation(); cancel()
        if (event.key === 'Enter') toggle()
        else save(event.key === 'Home' ? 0 : event.key === 'End' ? limit : event.key === 'ArrowDown' && collapsed ? minimumHeight : event.key === 'ArrowUp' && height <= minimumHeight ? 0 : bounded(height + (event.key === 'ArrowDown' ? 1 : -1) * (event.shiftKey ? 72 : 24), limit))
      }}>
      {collapsed ? <button className="studio-clip-player-reopen" type="button" aria-label={tr("展开播放器")} onClick={(event) => { event.stopPropagation(); toggle() }}><ChevronDown size={14} aria-hidden="true" /></button> : <GripHorizontal size={16} aria-hidden="true" />}
    </div>
  </>
}
