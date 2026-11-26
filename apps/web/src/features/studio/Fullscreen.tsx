import { tr, useLocale } from '../../ui/locale'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Expand, Minimize2 } from 'lucide-react'
import type { Selection } from './model'
import './Fullscreen.css'

type FullscreenMode = 'canvas' | 'memory'
type FullscreenState = { mode: FullscreenMode | null; enter: (mode: FullscreenMode) => void; exit: () => void }
const FullscreenContext = createContext<FullscreenState | null>(null)
export const useStudioFullscreen = () => useContext(FullscreenContext)

/** Keep the live canvas and inspector mounted inside one browser-fullscreen host. */
export function StudioShell({ children, className, selection, scope }: { children: ReactNode; className: string; selection: Selection | null; scope: string }) {
  useLocale()

  const root = useRef<HTMLDivElement>(null)
  const [mode, setMode] = useState<FullscreenMode | null>(null)
  const modeRef = useRef(mode)
  const native = useRef(false)
  const origin = useRef<HTMLElement | null>(null)
  const selectionRef = useRef(selection)
  selectionRef.current = selection
  const memorySelection = useRef(selection)
  const previousScope = useRef(scope)

  const enter = useCallback((next: FullscreenMode) => {
    if (!modeRef.current && document.activeElement instanceof HTMLElement) origin.current = document.activeElement
    if (next === 'memory') memorySelection.current = selectionRef.current
    modeRef.current = next; setMode(next)
    const element = root.current
    if (!element || document.fullscreenElement === element || !element.requestFullscreen) return
    // Embedded/mobile browsers can deny native fullscreen; the same view still
    // fills the page and retains its explicit exit control.
    void element.requestFullscreen().then(() => {
      native.current = document.fullscreenElement === element
      if (!modeRef.current && native.current) void document.exitFullscreen().catch(() => {})
    }).catch(() => {})
  }, [])
  const exit = useCallback(() => {
    modeRef.current = null; setMode(null)
    const element = root.current
    if (element?.contains(document.fullscreenElement)) void document.exitFullscreen().then(() => {
      // Some browsers pop the media player's fullscreen layer before the
      // enclosing Studio layer. An explicit Studio exit leaves both.
      if (!modeRef.current && document.fullscreenElement === element) return document.exitFullscreen()
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const changed = () => {
      if (root.current?.contains(document.fullscreenElement)) native.current = true
      else if (native.current) { native.current = false; modeRef.current = null; setMode(null) }
    }
    document.addEventListener('fullscreenchange', changed)
    return () => document.removeEventListener('fullscreenchange', changed)
  }, [])
  useEffect(() => {
    if (mode) root.current?.querySelector<HTMLButtonElement>(`${mode === 'memory' ? '.studio-memory-fullscreen ' : ''}[data-fullscreen-exit="${mode}"]`)?.focus({ preventScroll: true })
    else if (origin.current?.isConnected) { origin.current.focus({ preventScroll: true }); origin.current = null }
  }, [mode])
  useEffect(() => {
    if (previousScope.current !== scope) { previousScope.current = scope; exit() }
  }, [scope, exit])

  const value = useMemo(() => ({ mode, enter, exit }), [mode, enter, exit])
  return <FullscreenContext.Provider value={value}><div ref={root} className={className} data-studio-fullscreen={mode ?? undefined} data-studio-fullscreen-detail={mode === 'memory' && selection && selection !== memorySelection.current ? true : undefined} onKeyDown={(event) => {
    if (mode && event.key === 'Escape' && !event.defaultPrevented && !(event.target as Element).closest('dialog[open], .studio-inspector')) {
      event.preventDefault(); event.stopPropagation(); exit()
    }
  }}>{children}</div></FullscreenContext.Provider>
}

export function FullscreenControl({ mode, className = '' }: { mode: FullscreenMode; className?: string }) {
  useLocale()

  const fullscreen = useStudioFullscreen()
  if (!fullscreen) return null
  const active = fullscreen.mode === mode
  const title = active ? tr("退出全屏") : mode === 'canvas' ? tr("全屏画布") : tr("全屏查看全局记忆")
  return <button className={`studio-button studio-button--quiet studio-fullscreen-control ${className}`.trim()} type="button" aria-label={title} title={title} aria-pressed={active} data-fullscreen-exit={active ? mode : undefined} onClick={(event) => {
    event.stopPropagation()
    if (active) fullscreen.exit()
    else fullscreen.enter(mode)
  }}>{active ? <Minimize2 size={17} aria-hidden="true" /> : <Expand size={17} aria-hidden="true" />}<span>{title}</span></button>
}
