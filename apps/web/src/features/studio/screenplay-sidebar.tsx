import { tr } from '../../ui/locale'
import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { GripVertical } from 'lucide-react'
import { useHorizontalResize } from './useHorizontalResize'

export const SCREENPLAY_CHAT_WIDTH_KEY = 'studio:screenplay-chat-width'
export const screenplayChatKey = (projectId: string, frozen: boolean) => `studio:screenplay-chat:${projectId}:${frozen ? 'history' : 'draft'}`
const minimum = 260, maximum = 600, defaultWidth = 400

function readWidth() {
  try {
    const width = Number(localStorage.getItem(SCREENPLAY_CHAT_WIDTH_KEY))
    return Number.isFinite(width) && width >= minimum && width <= maximum ? width : null
  } catch { return null }
}

function readOpen(projectId: string, frozen: boolean) {
  try {
    const stored = localStorage.getItem(screenplayChatKey(projectId, frozen))
    if (stored === 'true' || stored === 'false') return stored === 'true'
  } catch { /* Reading and conversation work without local storage. */ }
  return !frozen && window.innerWidth > 900
}

export function useScreenplaySidebar(projectId: string, frozen: boolean) {
  const root = useRef<HTMLElement>(null)
  const [preference, setPreference] = useState(readWidth)
  const [available, setAvailable] = useState(() => window.innerWidth)
  const [opened, setOpened] = useState(() => ({ draft: readOpen(projectId, false), history: readOpen(projectId, true) }))
  const [visited, setVisited] = useState(!frozen)
  const mode = frozen ? 'history' : 'draft'
  const open = opened[mode]
  const limit = Math.max(minimum, Math.min(maximum, available - 520))

  const save = (next: number | null) => {
    setPreference(next)
    try {
      if (next === null) localStorage.removeItem(SCREENPLAY_CHAT_WIDTH_KEY)
      else localStorage.setItem(SCREENPLAY_CHAT_WIDTH_KEY, String(next))
    } catch { /* Keep the current in-memory preference. */ }
  }
  const { width, resizing, cancel, separator } = useHorizontalResize({
    preferredWidth: preference ?? defaultWidth, minimum, limit, save,
  })

  useEffect(() => {
    const resize = () => { cancel(); setAvailable(root.current?.getBoundingClientRect().width || window.innerWidth) }
    resize()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize)
    if (root.current) observer?.observe(root.current)
    window.addEventListener('resize', resize)
    return () => { observer?.disconnect(); window.removeEventListener('resize', resize) }
  }, [cancel])

  const toggle = () => {
    cancel()
    setVisited(true)
    setOpened({ ...opened, [mode]: !open })
    try { localStorage.setItem(screenplayChatKey(projectId, frozen), String(!open)) } catch { /* Optional preference. */ }
  }

  const handle = <div {...separator} className="studio-writing-resize" aria-label={tr("调整创作对话宽度")}><GripVertical size={14} aria-hidden="true" /></div>
  return { root, open, mounted: visited || open, toggle, handle, resizing, style: { '--writing-width': `${width}px` } as CSSProperties }
}
