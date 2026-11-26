import { tr, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { GripVertical } from 'lucide-react'
import { useHorizontalResize } from './useHorizontalResize'
import './ProjectOutline.css'

export const OUTLINE_WIDTH_STORAGE_KEY = 'studio:outline-width'
const minimum = 180
const maximum = 480

function storedWidth(): number | null {
  try {
    const raw = localStorage.getItem(OUTLINE_WIDTH_STORAGE_KEY)
    const value = raw === null ? NaN : Number(raw)
    return Number.isFinite(value) && value >= minimum && value <= maximum ? value : null
  } catch { return null }
}

export function ProjectOutlineFrame({ children }: { children: ReactNode }) {
  useLocale()

  const [preference, setPreference] = useState(storedWidth)
  const [viewport, setViewport] = useState(() => window.innerWidth)
  const pane = useRef<HTMLElement>(null)
  const limit = Math.max(minimum, Math.min(maximum, viewport - 560))

  const save = (next: number | null) => {
    setPreference(next)
    try {
      if (next === null) localStorage.removeItem(OUTLINE_WIDTH_STORAGE_KEY)
      else localStorage.setItem(OUTLINE_WIDTH_STORAGE_KEY, String(next))
    } catch { /* Resizing still works when local preferences are unavailable. */ }
  }

  const { width, resizing, cancel, separator } = useHorizontalResize({
    preferredWidth: preference ?? (viewport <= 1250 ? 206 : 232), minimum, limit, save,
    measureWidth: () => pane.current?.getBoundingClientRect().width,
  })

  useEffect(() => {
    const resize = () => { cancel(); setViewport(window.innerWidth) }
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [cancel])

  return <aside ref={pane} className={`studio-project-outline${resizing ? ' is-resizing' : ''}`} style={{ '--outline-width': `${width}px` } as CSSProperties} aria-label={tr("项目大纲")}>
    <div className="studio-outline-scroll">{children}</div>
    <div {...separator} className="studio-outline-resize" aria-label={tr("调整进度侧栏宽度")}><GripVertical size={14} aria-hidden="true" /></div>
  </aside>
}
