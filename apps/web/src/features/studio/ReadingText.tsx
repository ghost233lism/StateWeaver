import { tr, useLocale } from '../../ui/locale'
import { useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { entityExcerpt } from './entity-link-model'
import { Markdown } from './Markdown'
import './ReadingText.css'

/** An excerpt of the original, never a generated replacement for the source. */
export function readingExcerpt(text: string, limit = 240, lines = 5) {
  const firstLines = text.split('\n').slice(0, lines).join('\n')
  return entityExcerpt(firstLines, limit).trimEnd()
}

export function ReadingText({ text, limit = 300, lines = 6, storageKey, label = tr("全文"), className = '' }: {
  text: string; limit?: number; lines?: number; storageKey?: string; label?: string; className?: string
}) {
  useLocale()

  const [open, setOpen] = useState(() => {
    try { return storageKey ? localStorage.getItem(storageKey) === 'expanded' : false } catch { return false }
  })
  const id = useId()
  const body = useRef<HTMLDivElement>(null)
  const [clipped, setClipped] = useState(false)
  const preview = readingExcerpt(text, limit, lines)
  const long = preview !== text.trimEnd()
  useLayoutEffect(() => {
    if (open) return
    const content = body.current?.firstElementChild
    if (!(content instanceof HTMLElement)) return
    const measure = () => setClipped(content.scrollHeight > content.clientHeight + 1)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(content)
    return () => observer.disconnect()
  }, [open, preview, lines])
  return <div className={`studio-reading-text ${className}`}>
    <div ref={body} id={id} className={open ? '' : 'studio-reading-excerpt'} style={{ '--reading-lines': lines } as CSSProperties}><Markdown>{long && !open ? `${preview}…` : text}</Markdown></div>
    {(long || clipped || open) && <button className="studio-text-button" aria-expanded={open} aria-controls={id} onClick={() => {
      setOpen(!open)
      try { if (storageKey) localStorage.setItem(storageKey, open ? 'collapsed' : 'expanded') } catch { /* Reading works without local storage. */ }
    }}>{open ? tr("收起{{0}}", label) : tr("展开{{0}}", label)}</button>}
  </div>
}
