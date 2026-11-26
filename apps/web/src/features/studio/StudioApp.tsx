import { getLocale, tr, trSystem, useLocale } from '../../ui/locale'
import { useCallback, useEffect, useRef, useState, useMemo, type FormEvent } from 'react'
import { ArrowLeft, ChevronDown, Clapperboard, LoaderCircle, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { actionInput, confirmsCommand, isDefiniteCommandRejection, isStudioStorageId, mediaUrl, observeStudioRouteFailures, operationKey, studioConnection, studioRouteMismatch, StudioApiError, type ConnectionStatus, type RouteMismatch, type StudioClient, type StudioConnection, type StudioHealth } from './client'
import { effectiveProjectStatus, type CreateProject, type Selection, type ProjectSummary, type StudioEvent, type UserAction } from './model'
import { applyProjectEvent, mergeSnapshot, type ProjectCache } from './cache'
import { StudioDataContext, StudioMediaUrlContext } from './dataContext'
import { Status } from './Status'
import { LazyWorkspace, preloadWorkspace } from './WorkspaceLoader'
import type { StudioPage } from './StudioWorkspace'
import { EntityLinkProvider } from './EntityLinks'
import './studio.css'
import { ThemeProvider } from '../../ui/ThemeProvider'
import { AppearanceSettings } from './AppearanceSettings'
import { StudioShell } from './Fullscreen'
import { ProjectCover } from './ProjectCover'
import { DeleteProjectDialog } from './DeleteProjectDialog'
import { pendingCommands, type PendingCommands } from './pending-commands'

function linkedSelection(): Selection | null {
  try {
    const match = window.location.hash.match(/^#studio-entity\/([^?#]+)$/)
    const id = match ? decodeURIComponent(match[1]) : ''
    return id && id.length <= 256 ? { kind: 'entity', id } : null
  } catch { return null }
}
function storedExpanded(projectId: string): Set<string> {
  try { const value: unknown = JSON.parse(localStorage.getItem(`studio:expanded:${projectId}`) ?? '[]'); return new Set(Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : []) } catch { return new Set() }
}

type SessionProps = { client: StudioClient; connection: StudioConnection; storageId: string; pending: PendingCommands; onRouteMismatch: (reason: RouteMismatch) => void; healthNotice: string }
type RouteProbe = { client: StudioClient; controller: AbortController; pending?: Promise<StudioHealth | undefined> }
const wrongWorkflow = '当前 API 与 StateWeaver 不兼容。请检查 API 地址。'
const changedStorage = 'StateWeaver 存储已切换。重新连接后才能打开新工作区。'
const missingStorage = '当前 API 未提供 StateWeaver 存储身份。请检查 API 版本。'
const h3CapabilityLabels: Record<string, string> = {
  get 'endpoint-anchor-v1'() { return tr("端点锚定") },
  get 'tail-73'() { return tr("73 帧尾段") },
  get 'endpoint-only-v1'() { return tr("仅端点锚定编辑") },
}

function videoCapabilityWarning({ video_protocol: protocol, video_readiness: readiness, video_capacity_error: error, video_route_capacities: capacities }: StudioHealth): string | undefined {
  if (protocol === 'video-http-v1') {
    if (error === 'authentication') return tr("视频 API 鉴权失败，请检查凭证。")
    if (error === 'probe_in_progress') return tr("正在检查视频 API。")
    if (error) return tr("视频 API 配置或连接检查失败。")
    if (readiness && !readiness.default_ready) return tr("正在初始化视频 API。")
    return
  }
  switch (error) {
    case 'authentication': return tr("H3 鉴权失败，请检查视频服务凭证。")
    case 'upstream': return tr("H3 服务暂时不可用。")
    case 'network': return tr("无法连接 H3，请检查视频服务地址和网络。")
    case 'protocol': return tr("H3 返回的能力信息不兼容。")
    case 'probe_in_progress': return tr("正在检查 H3 视频服务能力。")
    case null:
    case undefined: break
    default: return tr("H3 视频服务能力检查失败。")
  }
  if (!readiness || readiness.default_ready) return
  if (readiness.missing.includes('workers')) {
    return capacities && Object.keys(capacities).length === 0
      ? tr("正在检查 H3 视频服务能力。")
      : tr("H3 暂无可用的 Ref2VA 工作线程。")
  }
  const missing = readiness.missing.map((capability) => h3CapabilityLabels[capability] ?? capability)
  return tr("H3 缺少默认制作所需能力（{{0}}），依赖这些能力的作业将等待。", missing.join('、'))
}

function runtimeHealthNotice(health: StudioHealth | undefined): string {
  if (!health) return ''
  const { provider_credentials: credentials, scheduler_error: schedulerError } = health
  const missing = []
  if (credentials?.missing.includes('text')) missing.push(tr("文本"))
  if (credentials?.missing.includes('image')) missing.push(tr("图片"))
  if (credentials?.missing.includes('video')) missing.push(tr("视频"))
  return [
    missing.length ? tr("缺少{{0}}服务凭证。", missing.join('、')) : '',
    credentials?.config_error ? tr("无法读取 AGENTVIDEO_SECRET_CONFIG_PATH 指定的凭证文件。") : '',
    schedulerError === 'CoordinatorUnavailable' ? tr("制作调度暂时无法连接协调服务。") : schedulerError ? tr("制作调度暂时异常，请检查服务状态。") : '',
    videoCapabilityWarning(health),
  ].filter(Boolean).join(' ')
}
const actorControlPath = (actorId: string, action: 'pause' | 'resume') => `actors/${encodeURIComponent(actorId)}/${action}`
const actionScope = (projectId: string, path: string, text = '', mode?: UserAction['director_mode']) => [projectId, path, text, mode ?? null]

export function StudioApp({ connection = studioConnection }: { connection?: StudioConnection }) {
  useLocale()

  const [checked, setChecked] = useState<{ connection: StudioConnection; error: string; storageId?: string; health?: StudioHealth } | null>(null)
  const [retry, setRetry] = useState(0)
  const onRouteMismatch = useCallback((reason: RouteMismatch) => {
    setChecked((current) => current?.connection === connection ? { connection, error: reason === 'storage-mismatch' ? changedStorage : wrongWorkflow } : current)
  }, [connection])

  useEffect(() => {
    const controller = new AbortController()
    void (async () => {
      try {
        const result = await connection.health(controller.signal)
        if (controller.signal.aborted) return
        const storageId = isStudioStorageId(result?.studio_storage_id) ? result.studio_storage_id : undefined
        if (!result || studioRouteMismatch(result)) setChecked({ connection, error: wrongWorkflow })
        else if (!storageId) setChecked({ connection, error: missingStorage })
        else setChecked({ connection, storageId, error: '', health: result })
      } catch (reason) {
        const error = reason instanceof DOMException && reason.name === 'TimeoutError'
          ? "连接工作室超时。请检查 StateWeaver 服务后重试。"
          : "无法连接 StateWeaver 服务。请检查连接后重试。"
        if (!controller.signal.aborted) setChecked({ connection, error })
      }
    })()
    return () => controller.abort()
  }, [connection, retry])

  const result = checked?.connection === connection ? checked : null
  const storageId = result?.storageId
  const health = result?.health
  const hasRuntimeHealth = health?.video_readiness != null || health?.provider_credentials != null || health?.video_capacity_error != null || health?.scheduler_error != null
  useEffect(() => {
    if (!storageId || !hasRuntimeHealth) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      try {
        const health = await connection.health(controller.signal)
        if (controller.signal.aborted) return
        const mismatch = studioRouteMismatch(health, storageId)
        if (mismatch) { onRouteMismatch(mismatch); return }
        setChecked((current) => (
          current?.connection === connection && current.storageId === storageId
            ? { ...current, health }
            : current
        ))
      } catch { /* Retain the last observation while health is unavailable. */ }
      if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 30_000)
    }
    timer = setTimeout(() => void refresh(), 30_000)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [connection, storageId, hasRuntimeHealth, onRouteMismatch])

  const client = useMemo(() => storageId ? connection.bindStorage(storageId) : undefined, [connection, storageId])
  // Keep the original bound cache keys so a tab can safely recover an operation
  // it sent before a reload, including when the reply was interrupted.
  const pending = useMemo(() => pendingCommands(storageId && `agentvideo:studio:v2:pending:v2:${storageId}`), [storageId])
  if (result && !result.error && storageId && client) return <StudioSession key={storageId} client={client} connection={connection} pending={pending} storageId={storageId} onRouteMismatch={onRouteMismatch} healthNotice={runtimeHealthNotice(health)} />
  return <ThemeProvider><main className="studio-root"><div className="studio-empty studio-page-loading">
    <h1>StateWeaver</h1>
    {result ? <><p role="alert">{trSystem(result.error)}</p><button className="studio-button" onClick={() => { setChecked(null); setRetry((value) => value + 1) }}>{tr("重试连接")}</button></>
      : <p role="status">{tr("正在连接工作室…")}</p>}
    {result && <AppearanceSettings />}
  </div></main></ThemeProvider>
}

function StudioSession({ client: transport, connection: server, pending, storageId, onRouteMismatch, healthNotice }: SessionProps) {
  useLocale()

  const [projectId, setProjectId] = useState(() => new URLSearchParams(window.location.search).get('project') ?? '')
  const [routeVersion, setRouteVersion] = useState(0)
  const [cache, setCache] = useState<ProjectCache | null>(null)
  const project = cache && cache.project.id === projectId ? cache.project : null
  const cacheRef = useRef(cache)
  cacheRef.current = cache
  const currentCache = useCallback(() => cacheRef.current, [])
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [listCursor, setListCursor] = useState<string | null>(null)
  const [listBack, setListBack] = useState<(string | undefined)[]>([])
  const [listAfter, setListAfter] = useState<string | undefined>()
  const [listMore, setListMore] = useState(false)
  const [page, setPage] = useState<StudioPage>('canvas')
  const [selection, setSelection] = useState<Selection | null>(linkedSelection)
  const [expanded, setExpanded] = useState<Set<string>>(() => storedExpanded(projectId))
  const [selectionBack, setSelectionBack] = useState<Selection[]>([])
  const [connection, setConnection] = useState<ConnectionStatus>('connecting')
  const [loading, setLoading] = useState(true)
  const [busyProjects, setBusyProjects] = useState<ReadonlySet<string>>(() => new Set())
  const [createBusy, setCreateBusy] = useState(false)
  const [createError, setCreateError] = useState('')
  const [error, setError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<ProjectSummary | null>(null)
  const [notice, setNotice] = useState('')
  const listRequest = useRef(0)
  const creating = useRef(false)
  const mutating = useRef(new Set<string>())
  const projectRef = useRef(project)
  projectRef.current = project
  const routeProjectRef = useRef(projectId)
  const routeEpoch = useRef(0)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const currentRoute = useCallback((id: string, epoch: number) => mounted.current && routeProjectRef.current === id && routeEpoch.current === epoch, [])

  useEffect(() => {
    // Font CSS and glyphs must not compete with the first project response.
    if (!loading) void import('../../fonts').catch(() => {})
  }, [loading])

  const applyRoute = useCallback((id: string) => {
    if (routeProjectRef.current !== id) { setRouteVersion(++routeEpoch.current); setLoading(true) }
    routeProjectRef.current = id
    setProjectId(id); setSelection(null); setSelectionBack([]); setError(''); setNotice('')
    setCreateOpen(false); setCreateError(''); setDeleteTarget(null)
  }, [])
  const navigate = useCallback((id: string, replace = false) => {
    const url = new URL(window.location.href)
    if (id) url.searchParams.set('project', id)
    else url.searchParams.delete('project')
    url.searchParams.delete('view')
    url.hash = ''
    if (replace) window.history.replaceState({}, '', url)
    else window.history.pushState({}, '', url)
    applyRoute(id)
  }, [applyRoute])

  const leaveDeletedProject = useCallback(() => {
    navigate('', true); setCache(null); setNotice("该项目已删除。")
  }, [navigate])

  const routeProbe = useRef<RouteProbe | null>(null)
  useEffect(() => {
    const probe = { client: transport, controller: new AbortController() }
    routeProbe.current = probe
    return () => { probe.controller.abort(); routeProbe.current = null }
  }, [transport, onRouteMismatch])

  const checkMissingRoute = useCallback(async (reason: unknown, signal?: AbortSignal) => {
    const probe = routeProbe.current
    if (probe?.client !== transport || signal?.aborted || !(reason instanceof StudioApiError)) return
    if (reason.status === 412) { onRouteMismatch('storage-mismatch'); return }
    if (reason.status !== 404) return
    const pending = probe.pending ??= (async () => {
      try { return await server.health(probe.controller.signal) }
      catch { return undefined }
    })()
    const health = await pending
    if (probe.pending === pending) probe.pending = undefined
    if (routeProbe.current !== probe || signal?.aborted || !health || typeof health.workflow !== 'string') return
    const mismatch = studioRouteMismatch(health, storageId)
    if (mismatch) onRouteMismatch(mismatch)
  }, [server, transport, storageId, onRouteMismatch])
  const client = useMemo(() => observeStudioRouteFailures(transport, checkMissingRoute), [transport, checkMissingRoute])

  useEffect(() => {
    const openLinkedEntity = () => { const next = linkedSelection(); if (next) { setPage('canvas'); setSelection(next); setSelectionBack([]) } }
    window.addEventListener('hashchange', openLinkedEntity)
    return () => window.removeEventListener('hashchange', openLinkedEntity)
  }, [])

  const refreshList = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++listRequest.current
    const epoch = routeEpoch.current
    const applicable = () => !signal?.aborted && requestId === listRequest.current && currentRoute('', epoch)
    setLoading(true)
    try {
      const result = await client.list(listAfter, signal)
      if (!applicable()) return
      if (!result.projects.length && listAfter && listBack.length) {
        setListAfter(listBack.at(-1)); setListBack(listBack.slice(0, -1)); return
      }
      setProjects(result.projects); setListCursor(result.next_cursor ?? null); setListMore(Boolean(result.has_more)); setError('')
    }
    catch (reason) {
      if (applicable()) {
        setError(reason instanceof Error ? reason.message : "项目加载失败")
      }
    }
    finally { if (applicable()) setLoading(false) }
  }, [client, currentRoute, listAfter, listBack])

  useEffect(() => { const controller = new AbortController(); if (!projectId) void refreshList(controller.signal); return () => controller.abort() }, [projectId, routeVersion, refreshList])

  const [invalidations, setInvalidations] = useState<Record<string, number>>({})
  const loadedScopes = useRef(new Set<string>())
  const pendingDetails = useRef(new Map<string, { key: string; controller: AbortController }>())
  useEffect(() => () => {
    for (const pending of pendingDetails.current.values()) pending.controller.abort()
    pendingDetails.current.clear()
    loadedScopes.current.clear()
  }, [projectId, routeVersion, client])
  const receive = useCallback((event: StudioEvent) => {
    if (event.project_id !== routeProjectRef.current) return
    if (event.kind === 'project_deleted') { leaveDeletedProject(); return }
    const { action, user_operation_key: operation } = event.data ?? {}
    const actorId = event.actor_id
    if ((action === 'pause' || action === 'resume') && typeof actorId === 'string' && typeof operation === 'string') {
      pending.forget('action', actionScope(event.project_id, actorControlPath(actorId, action)), operation)
    }
    setCache((previous) => previous ? applyProjectEvent(previous, event) : previous)
    const scopes = event.data?.invalidated
    if (Array.isArray(scopes)) setInvalidations((previous) => Object.fromEntries(Object.entries({ ...previous, ...Object.fromEntries(scopes.filter((scope) => typeof scope === 'string').map((scope) => [scope, Math.max(previous[scope] ?? 0, event.cursor)])) }).sort((a, b) => b[1] - a[1]).slice(0, 256)))
  }, [leaveDeletedProject, pending])

  useEffect(() => {
    if (!projectId) { setCache(null); return }
    preloadWorkspace()
    const controller = new AbortController()
    const epoch = routeEpoch.current
    const applicable = () => !controller.signal.aborted && currentRoute(projectId, epoch)
    let unsubscribe: (() => void) | undefined
    loadedScopes.current = new Set(); setInvalidations({}); setExpanded(storedExpanded(projectId)); setSelectionBack([])
    setCache(null); setLoading(true); setError('')
    void client.get(projectId, controller.signal).then((snapshot) => {
      if (!applicable()) return
      setCache(mergeSnapshot(null, snapshot))
      setPage(snapshot.project.status === 'screenwriting' ? 'screenplay' : 'canvas')
      unsubscribe = client.subscribe(projectId, (event) => { if (applicable()) receive(event) }, (status) => {
        if (!applicable()) return
        if (status === 'deleted') leaveDeletedProject()
        else if (status === 'workflow-mismatch' || status === 'storage-mismatch') onRouteMismatch(status)
        else if (status === 'unavailable') { setCache(null); setError("项目事件暂时不可用，请重新加载。") }
        else setConnection(status)
      }, snapshot.cursor)
    }).catch((reason: unknown) => {
      if (!applicable()) return
      if (reason instanceof StudioApiError && reason.status === 410) leaveDeletedProject()
      else setError(reason instanceof Error ? reason.message : "项目加载失败")
    })
      .finally(() => { if (applicable()) setLoading(false) })
    return () => { controller.abort(); unsubscribe?.() }
  }, [projectId, routeVersion, client, receive, leaveDeletedProject, currentRoute, onRouteMismatch])

  const detailScopes = useMemo(() => {
    const scopes = new Set<string>()
    for (const id of expanded) scopes.add(`set:${id}`)
    if (page === 'screenplay') scopes.add(page)
    if (selection?.kind === 'memory') scopes.add(`memory:${selection.id}`)
    if (selection?.kind === 'clip') scopes.add(`clip:${selection.id}`)
    if (selection?.kind === 'actor') scopes.add(`actor:${selection.id}`)
    if (selection?.kind === 'visual-task') scopes.add(`visual-task:${selection.id}`)
    return [...scopes].sort().join('|')
  }, [expanded, page, selection])
  useEffect(() => {
    if (!project || !projectId || project.id !== projectId) return
    const epoch = routeEpoch.current
    const activeScopes = new Set(detailScopes.split('|').filter(Boolean))
    for (const key of loadedScopes.current) if (!activeScopes.has(key.split('@')[0])) loadedScopes.current.delete(key)
    for (const [scope, pending] of pendingDetails.current) {
      if (!activeScopes.has(scope)) { pending.controller.abort(); pendingDetails.current.delete(scope) }
    }
    for (const scope of detailScopes.split('|').filter(Boolean)) {
      const key = `${scope}@${invalidations[scope] ?? 0}`
      if (loadedScopes.current.has(key)) continue
      const prior = pendingDetails.current.get(scope)
      if (prior?.key === key) continue
      prior?.controller.abort()
      for (const old of loadedScopes.current) if (old.startsWith(`${scope}@`)) loadedScopes.current.delete(old)
      const controller = new AbortController()
      const request = { key, controller }
      pendingDetails.current.set(scope, request)
      void client.details(projectId, scope, controller.signal).then((snapshot) => {
        if (!controller.signal.aborted && currentRoute(projectId, epoch) && pendingDetails.current.get(scope) === request) {
          pendingDetails.current.delete(scope); loadedScopes.current.add(key)
          setCache((previous) => mergeSnapshot(previous, snapshot))
        }
      }).catch((reason: unknown) => {
        if (pendingDetails.current.get(scope) === request) pendingDetails.current.delete(scope)
        if (!controller.signal.aborted && currentRoute(projectId, epoch)) setError(reason instanceof Error ? reason.message : "详情加载失败")
      })
    }
  }, [client, projectId, routeVersion, Boolean(project), currentRoute, detailScopes, invalidations])
  const dataSource = useMemo(() => ({ client, projectId, currentCache, invalidations, pending, checkMissingRoute }), [client, projectId, currentCache, invalidations, pending, checkMissingRoute])
  const loadLabels = useMemo(() => client.entityLabels ? (ids: string[], signal?: AbortSignal) => client.entityLabels!(projectId, ids, signal) : undefined, [client, projectId])

  useEffect(() => {
    const onPop = () => applyRoute(new URLSearchParams(window.location.search).get('project') ?? '')
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [applyRoute])

  const select = useCallback((next: Selection) => {
    setSelectionBack([]); setSelection(next)
    const current = projectRef.current
    if (next.kind === 'actor') {
      const setId = current?.actors?.[next.id]?.set_id
      if (setId) setExpanded((previous) => new Set(previous).add(setId))
    }
  }, [])

  useEffect(() => { if (project?.id === projectId) { try { localStorage.setItem(`studio:expanded:${projectId}`, JSON.stringify([...expanded])) } catch { /* Browser preferences may be disabled. */ } } }, [expanded, project?.id, projectId])
  const selectResult = (next: Selection) => { if (selection) setSelectionBack((previous) => [...previous, selection].slice(-32)); setSelection(next) }
  const goBack = selectionBack.length ? () => { setSelection(selectionBack.at(-1)!); setSelectionBack((previous) => previous.slice(0, -1)) } : undefined

  const toggle = useCallback((setId: string) => setExpanded((previous) => {
    const next = new Set(previous)
    if (next.has(setId)) next.delete(setId)
    else next.add(setId)
    return next
  }), [])

  const mutate = async (path: string, text = '', options?: Pick<UserAction, 'director_mode'>) => {
    const epoch = routeEpoch.current
    if (!project || !currentRoute(projectId, epoch) || mutating.current.has(projectId)) return false
    const intent = actionScope(projectId, path, text, options?.director_mode)
    const input = pending.get('action', intent) ?? { ...actionInput(project, text), ...options }
    mutating.current.add(projectId)
    setBusyProjects(new Set(mutating.current)); setError('')
    try {
      pending.save('action', intent, input)
      const next = await client.action(projectId, path, input)
      if (!confirmsCommand(next, projectId)) throw new Error("未能确认本次操作，请重试。")
      // The old view cannot clear a restored composer. Keep its exact retry key.
      if (!currentRoute(projectId, epoch)) return false
      pending.forget('action', intent, input.idempotency_key)
      next.events?.forEach(receive)
      setCache((previous) => previous?.project.id === projectId ? { ...previous, project: { ...previous.project, revision: Math.max(previous.project.revision, next.revision) } } : previous)
      return true
    } catch (reason) {
      if (isDefiniteCommandRejection(reason)) pending.forget('action', intent, input.idempotency_key)
      void checkMissingRoute(reason)
      if (!currentRoute(projectId, epoch)) return false
      setError(reason instanceof Error ? reason.message : "操作失败，请重试。")
      if (reason instanceof StudioApiError && reason.status === 409) {
        // Obtain the new revision even if live replay is temporarily unavailable.
        try {
          const latest = await client.get(projectId)
          if (currentRoute(projectId, epoch)) {
            setCache((previous) => previous?.project.id === projectId ? mergeSnapshot(previous, latest) : previous)
          }
        } catch { /* The stream may still bring the project up to date. */ }
      }
      return false
    } finally {
      mutating.current.delete(projectId)
      if (mounted.current) setBusyProjects(new Set(mutating.current))
    }
  }

  const productionLanguage = project ? project.config?.production_language ?? 'en' : undefined
  const control = (actorId: string, action: 'pause' | 'resume') => {
    if (!project || routeProjectRef.current !== projectId || mutating.current.has(projectId)) return
    const path = actorControlPath(actorId, action)
    const reverse = action === 'pause' ? 'resume' : 'pause'
    // The opposite state is already visible, and this click starts a new intent.
    pending.forget('action', actionScope(projectId, actorControlPath(actorId, reverse)))
    const intent = actionScope(projectId, path)
    const earlier = pending.get('action', intent)
    // At a newer revision an uncommitted command cannot apply, and an already
    // completed command cannot supply the opposite state now shown by the control.
    if (earlier && earlier.expected_revision < project.revision) pending.forget('action', intent, earlier.idempotency_key)
    void mutate(path)
  }

  const create = async (title: string, brief: string) => {
    const epoch = routeEpoch.current
    if (creating.current) return false
    creating.current = true
    setCreateBusy(true); setCreateError('')
    const intent = [title, brief]
    const input = pending.get('create', intent) ?? { title, brief, idempotency_key: operationKey(), config: { production_language: getLocale() } }
    try {
      pending.save('create', intent, input)
      const next = await client.create(input)
      if (!next.project?.id) throw new Error("未能确认项目创建，请重试。")
      if (!currentRoute(projectId, epoch)) return false
      pending.forget('create', intent, input.idempotency_key ?? undefined)
      navigate(next.project.id); return true
    } catch (reason) {
      if (isDefiniteCommandRejection(reason)) pending.forget('create', intent, input.idempotency_key ?? undefined)
      if (currentRoute(projectId, epoch)) setCreateError(reason instanceof Error ? reason.message : "项目创建失败")
      void checkMissingRoute(reason)
      return false
    }
    finally { creating.current = false; if (mounted.current) setCreateBusy(false) }
  }

  const content = <StudioDataContext.Provider value={dataSource}><StudioShell className={`studio-root${selection ? ' has-inspector' : ''}`} selection={selection} scope={`${projectId}:${page}`}>
    <header className="studio-topbar"><button className="studio-brand" onClick={() => navigate('')} aria-label={tr("返回项目列表")}><Clapperboard size={24} /><strong>StateWeaver</strong><span>{tr("创作室")}</span></button>
      {project && <><span className="studio-topbar-divider" /><button className="studio-project-switch" onClick={() => navigate('')}>{project.title}<ChevronDown size={14} /></button><Status value={effectiveProjectStatus(project)} /></>}
      <div className="studio-topbar-end">{project && <span className={`studio-connection studio-connection--${connection}`} role="status"><i />{connection === 'online' ? tr("实时同步") : connection === 'connecting' ? tr("连接中") : tr("重连中")}</span>}<AppearanceSettings productionLanguage={productionLanguage} /><button className="studio-button studio-button--quiet studio-create-trigger" aria-label={tr("新建项目")} onClick={() => setCreateOpen(true)}><Plus size={16} /><span>{tr("新建项目")}</span></button></div>
    </header>
    {healthNotice && <div className="studio-notice-banner studio-health-notice" role="status" tabIndex={0}><span>{healthNotice}</span></div>}
    {notice && <div className="studio-notice-banner" role="status"><span>{trSystem(notice)}</span><button className="studio-icon" onClick={() => setNotice('')} aria-label={tr("关闭通知")}><X size={16} /></button></div>}
    {error && !createOpen && <div className="studio-error-banner" role="alert"><span>{trSystem(error)}</span>{!projectId && <button className="studio-text-button" onClick={() => void refreshList()}><RefreshCw size={14} />{tr("重试")}</button>}<button className="studio-icon" onClick={() => setError('')} aria-label={tr("关闭提示")}><X size={16} /></button></div>}
    {projectId ? loading && !project ? <div className="studio-page-loading" role="status"><LoaderCircle className="studio-spinner" />{tr("正在加载项目…")}</div> : !project ? <div className="studio-empty studio-page-loading"><h1>{tr("项目暂时无法打开")}</h1><p>{tr("检查连接后重试，或返回项目列表选择其他项目。")}</p><button className="studio-button" onClick={() => navigate('')}><ArrowLeft size={16} />{tr("返回项目列表")}</button><button className="studio-button" onClick={() => window.location.reload()}>{tr("重新加载")}</button></div> : <LazyWorkspace client={client} project={project} page={page} selection={selection} expanded={expanded} busy={busyProjects.has(projectId)}
      onPage={(next) => { setPage(next); setSelection(null) }} onSelect={select} onSelectResult={selectResult} onToggle={toggle}
      onClose={() => { setSelection(null); setSelectionBack([]) }} onBack={goBack}
      onMessage={(text) => mutate('message', text)} onConfirm={(mode) => {
        const epoch = routeEpoch.current
        void mutate('confirm', '', mode ? { director_mode: mode } : undefined).then((ok) => { if (ok && currentRoute(projectId, epoch)) { setPage('canvas'); setSelection(null) } })
      }}
      onControl={control} /> : <ProjectLibrary projects={projects} loading={loading} onOpen={navigate} onCreate={() => setCreateOpen(true)} onDelete={client.deleteProject ? setDeleteTarget : undefined} hasMore={listMore} hasPrevious={listBack.length > 0} onNext={() => { setListBack([...listBack, listAfter]); setListAfter(listCursor ?? undefined) }} onPrevious={() => { setListAfter(listBack.at(-1)); setListBack(listBack.slice(0, -1)) }} />}
    {createOpen && <CreateProjectDialog busy={createBusy} error={createError} recovered={pending.latest('create', [])} onClose={() => { setCreateOpen(false); setCreateError('') }} onCreate={create} />}
    {deleteTarget && <DeleteProjectDialog key={deleteTarget.id} project={deleteTarget} client={client} commands={pending} onRequestError={checkMissingRoute} onClose={() => setDeleteTarget(null)} onDeleted={() => {
      if (!mounted.current) return
      const deleted = deleteTarget
      setProjects((previous) => previous.filter((item) => item.id !== deleted.id))
      setDeleteTarget(null); setNotice(`“${deleted.title}”已删除。`)
      if (routeProjectRef.current === deleted.id) leaveDeletedProject()
      else if (!routeProjectRef.current) void refreshList()
    }} />}
  </StudioShell></StudioDataContext.Provider>
  return <ThemeProvider><StudioMediaUrlContext.Provider value={client.mediaUrl ?? mediaUrl}>{project ? <EntityLinkProvider project={project} onSelect={selectResult} loadLabels={loadLabels}>{content}</EntityLinkProvider> : content}</StudioMediaUrlContext.Provider></ThemeProvider>
}

function ProjectLibrary({ projects, loading, onOpen, onCreate, onDelete, hasMore, hasPrevious, onNext, onPrevious }: { projects: ProjectSummary[]; loading: boolean; onOpen: (id: string) => void; onCreate: () => void; onDelete?: (project: ProjectSummary) => void; hasMore: boolean; hasPrevious: boolean; onNext: () => void; onPrevious: () => void }) {
  useLocale()

  return <main className="studio-library"><header><div><h1>{tr("把故事拍出来。")}</h1><p>{tr("从一段想法开始，和编剧定稿，再让导演团队完成制作。")}</p></div><button className="studio-button studio-button--primary" onClick={onCreate}><Plus size={18} />{tr("开始新故事")}</button></header>
    <section><h2>{tr("我的项目")}<span>{projects.length}</span></h2>{loading ? <div className="studio-page-loading" role="status"><LoaderCircle className="studio-spinner" />{tr("正在加载项目…")}</div> : projects.length ? <div className="studio-project-grid">{projects.map((project) => <article className="studio-project-card" key={project.id}><button className="studio-project-open" onPointerEnter={preloadWorkspace} onFocus={preloadWorkspace} onClick={() => onOpen(project.id)}><ProjectCover project={project} /><h3>{project.title}</h3><p>{project.brief}</p></button><footer><span>{project.set_count} {tr("个片场")}</span><time>{project.updated_at ? new Intl.DateTimeFormat(getLocale(), { month: 'short', day: 'numeric' }).format(new Date(project.updated_at)) : tr("刚刚创建")}</time>{onDelete && <button className="studio-project-delete" onClick={() => onDelete(project)} aria-label={tr("删除项目：{{0}}", project.title)}><Trash2 size={15} /><span>{tr("删除")}</span></button>}</footer></article>)}</div> : <div className="studio-library-empty"><Clapperboard size={48} /><h3>{tr("第一部作品，从一个想法开始")}</h3><p>{tr("写下人物、故事和想要的画面，编剧会和你一起把它变成剧本。")}</p><button className="studio-button" onClick={onCreate}>{tr("创建第一个项目")}</button></div>}</section><div className="studio-pagination">{hasPrevious && <button className="studio-button" onClick={onPrevious} disabled={loading}>{tr("上一页项目")}</button>}{hasMore && <button className="studio-button" onClick={onNext} disabled={loading}>{tr("下一页项目")}</button>}</div>
  </main>
}

function CreateProjectDialog({ busy, error, recovered, onClose, onCreate }: { busy: boolean; error: string; recovered?: CreateProject; onClose: () => void; onCreate: (title: string, brief: string) => Promise<boolean> }) {
  useLocale()

  const [title, setTitle] = useState(recovered?.title ?? '')
  const [brief, setBrief] = useState(recovered?.brief ?? '')
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { ref.current?.showModal(); return () => ref.current?.close() }, [])
  const submit = (event: FormEvent) => { event.preventDefault(); if (title.trim() && brief.trim()) void onCreate(title.trim(), brief.trim()) }
  return <dialog ref={ref} className="studio-create-dialog" onCancel={(event) => { event.preventDefault(); if (!busy) onClose() }} aria-labelledby="studio-create-title"><form onSubmit={submit}><header><div><h2 id="studio-create-title">{tr("开始一个新故事")}</h2><p>{tr("把你的想法交给编剧。")}</p></div><button className="studio-icon" type="button" disabled={busy} onClick={onClose} aria-label={tr("关闭新建项目")}><X size={20} /></button></header><label htmlFor="studio-title">{tr("项目名称")}</label><input id="studio-title" autoFocus required maxLength={160} value={title} onChange={(event) => setTitle(event.target.value)} placeholder={tr("给这个故事起个名字")} disabled={busy} /><label htmlFor="studio-brief">{tr("你想拍什么？")}</label><textarea id="studio-brief" required rows={6} value={brief} onChange={(event) => setBrief(event.target.value)} placeholder={tr("故事发生在哪里？有哪些人物？你希望观众看到什么？")} disabled={busy} />{error && <p role="alert" className="studio-error-text">{trSystem(error)}</p>}<footer><span>{tr("确认剧本前，你可以继续修改创作要求。")}</span><button className="studio-button studio-button--primary" disabled={busy || !title.trim() || !brief.trim()} type="submit">{busy ? tr("正在创建…") : tr("交给编剧")}</button></footer></form></dialog>
}
