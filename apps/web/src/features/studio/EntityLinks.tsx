import { tr, useLocale } from '../../ui/locale'
import { createContext, Fragment, useCallback, useContext, useEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from 'react'
import { entityLabel, entityTarget, entityTextParts, needsEntityLabel } from './entity-link-model'
import { EntityLabelCache, emptyLabelSnapshot, type EntityLabelLoader } from './entityLabelCache'
import type { Selection, StudioProject } from './model'
import './EntityLinks.css'

type EntityLinkContext = { project: StudioProject; onSelect: (selection: Selection) => void; labels?: EntityLabelCache }
const Context = createContext<EntityLinkContext | null>(null)

export function EntityLinkProvider({ project, onSelect, children, loadLabels }: Omit<EntityLinkContext, 'labels'> & { children: ReactNode; loadLabels?: EntityLabelLoader }) {
  useLocale()

  const parent = useContext(Context)
  const inherited = project.id && parent?.project.id === project.id ? parent.labels : undefined
  const labels = useMemo(() => inherited ?? (loadLabels ? new EntityLabelCache(loadLabels) : undefined), [inherited, project.id, loadLabels])
  useEffect(() => labels?.attach(), [labels])
  const value = useMemo(() => ({ project, onSelect, labels }), [project, onSelect, labels])
  return <Context.Provider value={value}>{children}</Context.Provider>
}

export const useEntityLinks = () => useContext(Context)

function useVisibleLabel(id: string, cache: EntityLabelCache | undefined) {
  const element = useRef<HTMLElement | null>(null)
  const subscribe = useCallback((listener: () => void) => cache?.subscribe(id, listener) ?? (() => {}), [cache, id])
  const snapshot = useCallback(() => cache?.getSnapshot(id) ?? emptyLabelSnapshot(), [cache, id])
  const state = useSyncExternalStore(subscribe, snapshot, emptyLabelSnapshot)
  const ref = useCallback((node: HTMLElement | null) => { element.current = node }, [])
  useEffect(() => {
    const node = element.current
    if (!cache || !node) return
    let release: (() => void) | undefined
    const visible = (near: boolean) => {
      if (near && !release) release = cache.watch(id)
      else if (!near && release) { release(); release = undefined }
    }
    if (typeof IntersectionObserver === 'undefined') {
      const check = () => { const bounds = node.getBoundingClientRect(); visible(bounds.width > 0 && bounds.height > 0 && bounds.bottom >= -160 && bounds.top <= window.innerHeight + 160 && bounds.right >= -160 && bounds.left <= window.innerWidth + 160) }
      check(); window.addEventListener('scroll', check, true); window.addEventListener('resize', check)
      return () => { release?.(); window.removeEventListener('scroll', check, true); window.removeEventListener('resize', check) }
    }
    const observer = new IntersectionObserver((entries) => visible(entries.some((entry) => entry.isIntersecting)), { rootMargin: '160px 0px' })
    observer.observe(node)
    return () => { observer.disconnect(); release?.() }
  }, [cache, id])
  return { state, ref }
}

function EntityName({ id, children, interactive = true }: { id: string; children?: ReactNode; interactive?: boolean }) {
  useLocale()

  const context = useEntityLinks()
  const target = context ? entityTarget(context.project, id) : null
  const cache = children === undefined && context && target && needsEntityLabel(context.project, id) ? context.labels : undefined
  const { state, ref } = useVisibleLabel(id, cache)
  const local = context ? entityLabel(context.project, id) : tr("关联记录")
  const label = children ?? (state.status === 'ready' ? state.value.label : state.status === 'missing' ? tr("记录已不可用") : state.status === 'error' ? tr("名称加载失败") : cache && local === tr('关联记录') ? tr("加载记录名称…") : local)
  const title = state.status === 'ready' ? state.value.description || undefined : state.status === 'missing' ? tr("当前项目中未找到这条原始记录。") : state.status === 'error' ? tr("暂时无法加载名称，仍可打开原始记录。") : undefined
  const properties = { ref, title, 'data-label-state': cache ? state.status : undefined, 'aria-busy': state.status === 'loading' || undefined }
  if (!interactive || !context || !target) return <span className="studio-entity-name" {...properties}>{label}</span>
  return <a className="studio-entity-link nodrag nopan" href={`#studio-entity/${encodeURIComponent(id)}`} {...properties} onClick={(event) => {
    if (event.defaultPrevented) return
    event.stopPropagation()
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault(); context.onSelect(target)
  }} onAuxClick={(event) => event.stopPropagation()}>{label}</a>
}

export function EntityLink({ id, children }: { id: string; children?: ReactNode }) {
  useLocale()

  return <EntityName id={id}>{children}</EntityName>
}

export function EntityText({ children, interactive = true }: { children: string; interactive?: boolean }) {
  useLocale()

  const context = useEntityLinks()
  if (!context) return <>{children}</>
  return <>{entityTextParts(children, context.project).map((part) => part.id
    ? <EntityName key={part.start} id={part.id} interactive={interactive} />
    : <Fragment key={part.start}>{part.text}</Fragment>)}</>
}

export { entityLabel, entityTarget, entityExcerpt } from './entity-link-model'
