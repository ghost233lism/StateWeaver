import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { StudioClient } from './client'
import type { BodyChunk, StudioProject } from './model'
import { hydrateOlderJobSubmission, isCurrentMemoryProjection, mergeEntityProjection, mergeFinalProjection } from './cache'
import { actionLabels, DetailValue } from './DetailValue'
import { Markdown } from './Markdown'
import { useStudioData } from './dataContext'

export { StudioDataContext, useStudioData } from './dataContext'

/** One open card owns one immutable body snapshot; live updates never replace it. */
export function BodyReader({ bodyRef }: { bodyRef: string }) {
  useLocale()

  const source = useStudioData()
  return source ? <BodySession key={source.projectId} client={source.client} projectId={source.projectId} bodyRef={bodyRef} /> : null
}

function BodySession({ client, projectId, bodyRef }: { client: StudioClient; projectId: string; bodyRef: string }) {
  useLocale()

  const pinnedRef = useRef(bodyRef)
  const snapshotResolved = useRef(false)
  const [chunk, setChunk] = useState<BodyChunk | null>(null)
  const [offset, setOffset] = useState(0)
  const [back, setBack] = useState<number[]>([])
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setChunk(null); setError('')
    // React's development effect replay can cancel before an expensive live snapshot starts.
    void Promise.resolve().then(async () => {
      if (controller.signal.aborted) return
      const requestedRef = pinnedRef.current
      const result = await client.body(projectId, requestedRef, offset, controller.signal)
      if (controller.signal.aborted) return
      if (requestedRef.startsWith('live:') && !requestedRef.startsWith('live:snapshot:') && !result.body_ref) throw new Error("正文快照暂不可用，请关闭后重新打开。")
      if (snapshotResolved.current && result.body_ref && result.body_ref !== requestedRef) throw new Error("正文快照不一致，请关闭后重新打开。")
      if (!snapshotResolved.current) { pinnedRef.current = result.body_ref ?? requestedRef; snapshotResolved.current = true }
      setChunk(result)
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "正文加载失败") })
    return () => controller.abort()
  }, [client, projectId, offset, retry])
  return <div className="studio-body-reader">{error ? <p role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((value) => value + 1)}>{tr("重试")}</button></p> : !chunk ? <p role="status">{tr("正在加载完整内容…")}</p> : <><BodyContent chunk={chunk} /><div className="studio-pagination">{back.length > 0 && <button className="studio-text-button" onClick={() => { setOffset(back.at(-1)!); setBack(back.slice(0, -1)) }}>{tr("上一段")}</button>}{chunk.next_offset != null && <button className="studio-text-button" onClick={() => { setBack([...back, offset]); setOffset(chunk.next_offset!) }}>{tr("继续读取")}</button>}<small>{chunk.total ? chunk.offset + 1 : 0}–{Math.min(chunk.offset + Array.from(chunk.text).length, chunk.total)} / {chunk.total} {tr("字符")}</small></div></>}</div>
}

function BodyContent({ chunk }: { chunk: BodyChunk }) {
  useLocale()

  if (chunk.content_type !== 'text/plain' && chunk.offset === 0 && chunk.next_offset == null) {
    try {
      const body: unknown = JSON.parse(chunk.text)
      if (body && typeof body === 'object' && !Array.isArray(body) && structuredWithinLimit(body)) {
        const value = body as Record<string, unknown>
        if (typeof value.text === 'string' && value.data && typeof value.data === 'object') return <div className="studio-body-content">{value.text && <Markdown>{actionLabels[value.text] ?? value.text}</Markdown>}{Object.keys(value.data).length > 0 && <DetailValue value={value.data} />}</div>
      }
    } catch { /* A partial or malformed archived result remains available verbatim. */ }
  }
  return chunk.content_type === 'text/plain' ? <Markdown>{chunk.text}</Markdown> : <pre>{chunk.text}</pre>
}

/** Small text can encode a very broad/deep tree; bound structured DOM independently. */
function structuredWithinLimit(value: unknown) {
  const pending = [{ value, depth: 0 }]
  let visited = 0
  while (pending.length) {
    const item = pending.pop()!
    if (++visited > 500 || item.depth > 12) return false
    if (item.value && typeof item.value === 'object') {
      const children = Object.values(item.value)
      if (children.length + visited > 500) return false
      for (const child of children) pending.push({ value: child, depth: item.depth + 1 })
    }
  }
  return true
}

export function LazyDetails({ title, children, className = 'studio-record' }: { title: ReactNode; children: ReactNode; className?: string }) {
  useLocale()

  const [open, setOpen] = useState(false)
  return <details className={className} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}><summary onClick={(event) => { event.preventDefault(); setOpen((value) => !value) }}>{title}</summary>{open && children}</details>
}

export function useEntityHistory(project: import('./model').StudioProject, kind: 'references' | 'states' | 'candidates' | 'memory_entries' | 'usage' | 'reference_plans' | 'visual_tasks', parentId: string) {
  const source = useStudioData()
  const [page, setPage] = useState<import('./model').EntityPage | null>(null)
  const [after, setAfter] = useState<string | undefined>()
  const [back, setBack] = useState<(string | undefined)[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => { setAfter(undefined); setBack([]); setPage(null) }, [kind, parentId])
  useEffect(() => {
    if (!source || !parentId) return
    const controller = new AbortController()
    setLoading(true); setError('')
    void source.client.history(source.projectId, kind, parentId, after, controller.signal).then((result) => {
      if (!controller.signal.aborted) setPage(result)
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "历史加载失败") }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [source?.client, source?.projectId, source?.invalidations?.[kind === 'memory_entries' ? `memory:${parentId}` : kind === 'candidates' ? `clip:${parentId}` : kind === 'usage' ? `usage:${parentId}` : kind === 'visual_tasks' ? `visual-tasks:${parentId}` : 'memory:global'], kind, parentId, after, retry])
  const versions = source?.currentCache?.()?.versions
  const enriched = useMemo(() => {
    if (!page) return project
    const collectionKind = kind === 'usage' ? 'candidates' : kind
    const additions = { ...page.related, [collectionKind]: { ...page.related?.[collectionKind], ...Object.fromEntries(page.items.map((item) => [String(item.id), item])) } }
    const result = { ...project }
    for (const [collection, items] of Object.entries(additions)) {
      if ((collection in project || collection === 'visual_tasks') && items && typeof items === 'object') Object.assign(result, { [collection]: mergeRelated(project, collection, items, page.entity_versions, versions, page.cursor) })
    }
    return result
  }, [project, page, kind, versions])
  const ids = useMemo(() => page?.items.map((item) => String(item.id)), [page])
  return {
    project: enriched,
    items: page?.items,
    ids,
    ancestorIds: page?.ancestor_ids ?? [],
    controls: <div className="studio-pagination">{loading && <span role="status">{tr("正在加载历史…")}</span>}{error && <span role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((value) => value + 1)}>{tr("重试")}</button></span>}{back.length > 0 && <button className="studio-text-button" disabled={loading} onClick={() => { setAfter(back.at(-1)); setBack(back.slice(0, -1)) }}>{tr("上一页历史")}</button>}{page?.has_more && <button className="studio-text-button" disabled={loading} onClick={() => { setBack([...back, after]); setAfter(page.next_cursor ?? undefined) }}>{tr("下一页历史")}</button>}</div>,
  }
}

export function useEntityDetail(project: StudioProject, scope: string | undefined) {
  const source = useStudioData()
  const [detail, setDetail] = useState<import('./model').StudioSnapshot | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    setDetail(null)
    if (!source || !scope) return
    const controller = new AbortController()
    setLoading(true); setError('')
    void source.client.details(source.projectId, scope, controller.signal).then((result) => { if (!controller.signal.aborted) setDetail(result) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "详情加载失败") })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [source?.client, source?.projectId, source?.invalidations?.[scope?.startsWith('preparation-history:') ? scope.replace('preparation-history:', 'clip:') : scope ?? ''], scope?.startsWith('asset-tree:') ? source?.invalidations?.['asset-trees'] : undefined, scope === 'final' ? project.final_media_id : undefined, scope, retry])
  const liveCache = source?.currentCache?.()
  // A canvas gesture changes no entity data. Keep the merged projection stable
  // until an actual source/detail/version changes, including stale-page fences.
  const enriched = useMemo(() => {
    if (!detail || detail.scope !== scope) return project
    const result = { ...project }
    for (const collection of ['candidates', 'references', 'media', 'assets', 'states', 'clips', 'sets', 'actors', 'jobs', 'transcript', 'proposals', 'story_events', 'reference_plans', 'visual_tasks'] as const) result[collection] = mergeRelated(project, collection, detail.project[collection] ?? {}, detail.entity_versions, liveCache?.versions, detail.cursor, scope) as never
    const exactEntry = scope?.startsWith('entity:') || scope?.startsWith('memory-entry:') ? scope.slice(scope.indexOf(':') + 1) : undefined
    const hasExactMemory = exactEntry && detail.project.global_memory?.story?.some((entry) => entry.id === exactEntry)
    const fullMemory = scope === 'memory:global'
    if ((fullMemory || hasExactMemory) && isCurrentMemoryProjection(project, detail, fullMemory ? liveCache : undefined)) result.global_memory = detail.project.global_memory
    if (scope === 'final') Object.assign(result, mergeFinalProjection(project, detail, liveCache))
    return result
  }, [project, detail, scope, liveCache?.versions, liveCache?.snapshotCursor, liveCache?.coreVersions])
  return { project: enriched, stateTree: detail && detail.scope === scope ? detail.state_tree : undefined, loading, error, loaded: Boolean(detail && detail.scope === scope), status: <>{loading && <p role="status">{tr("正在加载详情…")}</p>}{error && <p role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((value) => value + 1)}>{tr("重试")}</button></p>}</> }
}

function mergeRelated(project: StudioProject, collection: string, incoming: object, incomingVersions?: Record<string, number>, versions?: Record<string, number>, cursor = 0, scope = 'history') {
  const original = project[collection as keyof StudioProject] as Record<string, unknown> | undefined
  let next = original ?? {}
  const assign = (id: string, value: unknown) => {
    if (value === next[id]) return
    if (next === original) next = { ...original }
    next[id] = value
  }
  for (const [id, item] of Object.entries(incoming)) {
    if ((incomingVersions?.[`${collection}:${id}`] ?? cursor) < (versions?.[`${collection}:${id}`] ?? -1)) {
      const hydrated = hydrateOlderJobSubmission(collection, next[id], item, scope)
      assign(id, hydrated)
      continue
    }
    assign(id, mergeEntityProjection(collection, next[id], item, scope))
  }
  return next
}
