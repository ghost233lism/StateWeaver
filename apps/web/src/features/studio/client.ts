import type { CreateProject, EventBatch, ProjectList, StudioEvent, StudioProject, StudioSnapshot, CommandResult, TranscriptPage, EntityPage, EntityLabelBatch, BodyChunk, UserAction, WorkspaceLayout, WorkspaceLayoutUpdate, ClipReadiness, CandidateComparison, ClipPreparationUpdate, SetPreview, SetDependencyReport, ProductionStatus } from './model'

const studioMediaPath = '/api/v1/studio/projects'
const healthWaitMilliseconds = 8_000
const writeWaitMilliseconds = 30_000

export type H3VideoReadiness = {
  default_ready: boolean
  continuation_ready: boolean
  anchored_edit_ready: boolean
  missing: string[]
}
export type StudioHealth = {
  workflow: string
  workspace_schema?: string
  studio_storage_id?: string
  provider_credentials?: { missing: ('text' | 'image' | 'video')[]; config_error: string | null } | null
  video_protocol?: 'h3-ref2va-v1' | 'video-http-v1' | null
  video_capacity_source?: 'advertised' | 'configured' | null
  video_readiness?: H3VideoReadiness | null
  video_capacity_error?: string | null
  video_route_capacities?: Record<string, number> | null
  scheduler_error?: string | null
}
export type RouteMismatch = 'workflow-mismatch' | 'storage-mismatch'

export function studioRouteMismatch(health: StudioHealth, storageId?: string): RouteMismatch | undefined {
  if (health.workflow !== 'agentvideo-v2' || health.workspace_schema !== 'stateweaver-release-v1') return 'workflow-mismatch'
  if (storageId && health.studio_storage_id !== storageId) return 'storage-mismatch'
}

export function isStudioStorageId(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{32}$/.test(value)
}

export interface StudioConnection {
  health(signal?: AbortSignal): Promise<StudioHealth>
  bindStorage(storageId: string): StudioClient
}

export interface StudioClient {
  mediaUrl?(url: string): string
  productionStatus?(projectId: string, details: boolean, signal?: AbortSignal): Promise<ProductionStatus>
  setDependencies?(projectId: string, setId: string, signal?: AbortSignal): Promise<SetDependencyReport>
  setPreview?(projectId: string, setId: string, options: { prepare?: boolean; retry?: boolean }, signal?: AbortSignal): Promise<SetPreview>
  configurePreparation?(projectId: string, clipId: string, input: ClipPreparationUpdate): Promise<CommandResult>
  readiness?(projectId: string, clipId: string, signal?: AbortSignal): Promise<ClipReadiness>
  comparison?(projectId: string, clipId: string, after?: string, signal?: AbortSignal): Promise<CandidateComparison>
  memory?(projectId: string, scope: string, status: 'effective' | 'all', after?: string, signal?: AbortSignal): Promise<EntityPage>
  entityLabels?(projectId: string, ids: string[], signal?: AbortSignal): Promise<EntityLabelBatch>
  layout?(projectId: string, signal?: AbortSignal): Promise<WorkspaceLayout>
  saveLayout?(projectId: string, input: WorkspaceLayoutUpdate, signal?: AbortSignal): Promise<WorkspaceLayout>
  list(after?: string, signal?: AbortSignal): Promise<ProjectList>
  create(input: CreateProject): Promise<StudioSnapshot>
  deleteProject?(projectId: string, input: UserAction): Promise<CommandResult>
  get(projectId: string, signal?: AbortSignal): Promise<StudioSnapshot>
  details(projectId: string, scope: string, signal?: AbortSignal): Promise<StudioSnapshot>
  transcript(projectId: string, actorId: string, before?: number, signal?: AbortSignal): Promise<TranscriptPage>
  body(projectId: string, ref: string, offset?: number, signal?: AbortSignal): Promise<BodyChunk>
  history(projectId: string, kind: 'references' | 'states' | 'candidates' | 'jobs' | 'memory_entries' | 'usage' | 'reference_plans' | 'visual_tasks', parentId: string, after?: string, signal?: AbortSignal): Promise<EntityPage>
  action(projectId: string, path: string, input: UserAction): Promise<CommandResult>
  subscribe(projectId: string, onEvent: (event: StudioEvent) => void, onStatus: (status: ConnectionStatus) => void, cursor: number): () => void
}

export type ConnectionStatus = 'connecting' | 'online' | 'reconnecting' | 'deleted' | 'unavailable' | RouteMismatch

export class StudioApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

const observedRequests = new Set<PropertyKey>([
  'list', 'get', 'details', 'productionStatus', 'setDependencies', 'setPreview', 'readiness', 'comparison',
  'memory', 'entityLabels', 'layout', 'transcript', 'body', 'history',
  'saveLayout',
] satisfies (keyof StudioClient)[])

/** Studio reads and workspace saves share the active V2 route check. */
export function observeStudioRouteFailures(client: StudioClient, onError: (reason: unknown, signal?: AbortSignal) => void): StudioClient {
  const methods = new Map<PropertyKey, { original: unknown; wrapped: unknown }>()
  return new Proxy({} as StudioClient, {
    get(_target, key) {
      const method = Reflect.get(client, key)
      if (typeof method !== 'function') return method
      const previous = methods.get(key)
      if (previous && previous.original === method) return previous.wrapped
      const wrapped = observedRequests.has(key) ? async (...args: unknown[]) => {
        try { return await Reflect.apply(method, client, args) }
        catch (reason) {
          const last = args.at(-1)
          onError(reason, last instanceof AbortSignal ? last : undefined)
          throw reason
        }
      } : method.bind(client)
      methods.set(key, { original: method, wrapped })
      return wrapped
    },
  })
}

export function isDefiniteCommandRejection(reason: unknown): boolean {
  return reason instanceof StudioApiError && [400, 401, 403, 409, 410, 422].includes(reason.status)
}

export function confirmsCommand(result: CommandResult | null, projectId: string): boolean {
  return result?.project_id === projectId && Number.isInteger(result.revision)
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try { return await requestOnce<T>(url, init) }
    catch (error) {
      const transient = error instanceof StudioApiError ? [408, 429, 500, 502, 503, 504].includes(error.status) : error instanceof TypeError
      if ((init?.method ?? 'GET').toUpperCase() !== 'GET' || init?.signal?.aborted || !transient || attempt >= 2) throw error
      await new Promise<void>((resolve, reject) => {
        const signal = init?.signal
        const abort = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(new DOMException('Aborted', 'AbortError')) }
        const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, [350, 1200][attempt])
        if (signal?.aborted) abort()
        else signal?.addEventListener('abort', abort, { once: true })
      })
    }
  }
}

async function requestOnce<T>(url: string, init?: RequestInit): Promise<T> {
  if (!init?.method || ['GET', 'HEAD'].includes(init.method.toUpperCase())) return readResponse<T>(url, init)
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (init.signal?.aborted) abort()
  else init.signal?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timer = setTimeout(() => { timedOut = true; abort() }, writeWaitMilliseconds)
  try {
    return await readResponse<T>(url, { ...init, signal: controller.signal })
  } catch (error) {
    if (timedOut) throw new Error("暂未收到操作回执。请核对后重试。")
    throw error
  } finally { clearTimeout(timer); init.signal?.removeEventListener('abort', abort) }
}

async function readResponse<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json() as { detail?: unknown; message?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
      else if (typeof body.message === 'string') detail = body.message
    } catch { /* A proxy may return HTML. Keep the actionable HTTP status. */ }
    throw new StudioApiError(response.status === 409 ? "项目刚刚更新，请检查最新内容后重试。" : (detail || `请求失败（${response.status}），请稍后重试。`), response.status)
  }
  return response.json() as Promise<T>
}

const post = <T,>(url: string, input: unknown) => request<T>(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
})

export function operationKey(random: Crypto = globalThis.crypto): string {
  if (typeof random.randomUUID === 'function') return random.randomUUID()
  const bytes = random.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-')
}

export function actionInput(project: StudioProject, text = ''): UserAction {
  return { expected_revision: project.revision, idempotency_key: operationKey(), text }
}

export function mediaUrl(url: string, path?: string): string {
  // Media written by the API carries a stable, origin-relative project address.
  // Only the active storage's transport can turn that address into a bound URL.
  if (path && url.startsWith(`${studioMediaPath}/`)) return `${path}${url.slice(studioMediaPath.length)}`
  return url
}

/** Replay before every connection closes the gap while the socket was unavailable. */
export function subscribeToProject(
  projectId: string,
  onEvent: (event: StudioEvent) => void,
  onStatus: (status: ConnectionStatus) => void,
  transport: {
    batch: (after: number) => Promise<EventBatch>
    socket: (after: number) => WebSocket
    delay?: number
    cursor?: number
    verifyWorkflow?: (signal: AbortSignal) => Promise<boolean | RouteMismatch>
  },
) {
  let cursor = transport.cursor ?? 0
  let disposed = false
  let socket: WebSocket | undefined
  let retry: ReturnType<typeof setTimeout> | undefined
  let missingRoutes = 0
  let failedHandshakes = 0
  const controller = new AbortController()
  const releaseSocket = (close: boolean) => {
    const current = socket
    socket = undefined
    if (!current) return
    current.onclose = null; current.onerror = null; current.onopen = null; current.onmessage = null
    if (close) current.close()
  }
  const dispose = () => {
    disposed = true
    controller.abort()
    if (retry !== undefined) clearTimeout(retry)
    releaseSocket(true)
  }
  const verifyWorkflow = async () => {
    try { return await transport.verifyWorkflow?.(controller.signal) }
    catch { return undefined }
  }
  const stop = (status: ConnectionStatus) => {
    dispose()
    onStatus(status)
  }
  const accept = (event: StudioEvent) => {
    if (disposed || event.project_id !== projectId || !Number.isInteger(event.cursor) || event.cursor <= cursor) return
    cursor = event.cursor
    onEvent(event)
    if (event.kind === 'project_deleted') dispose()
  }
  const reconnect = () => {
    if (disposed || retry !== undefined) return
    onStatus('reconnecting')
    retry = setTimeout(() => { retry = undefined; void connect() }, transport.delay ?? 2000)
  }
  const connect = async () => {
    if (disposed) return
    try {
      if (transport.verifyWorkflow && failedHandshakes >= 2) {
        failedHandshakes = 0
        const valid = await verifyWorkflow()
        if (disposed) return
        if (valid !== undefined && valid !== true) { stop(valid === false ? 'workflow-mismatch' : valid); return }
      }
      let more = true
      while (more) {
        const before = cursor
        const batch = await transport.batch(cursor)
        if (disposed) return
        batch.events.sort((a, b) => a.cursor - b.cursor).forEach(accept)
        if (disposed) return
        cursor = Math.max(cursor, batch.cursor)
        more = Boolean(batch.has_more)
        if (more && cursor <= before) throw new Error("事件分页未前进")
      }
      missingRoutes = 0
      const current = socket = transport.socket(cursor)
      let connected = false
      const disconnect = (close: boolean) => {
        if (disposed || socket !== current) return
        if (!connected) failedHandshakes++
        releaseSocket(close)
        reconnect()
      }
      current.onopen = () => {
        if (disposed || socket !== current) return
        connected = true
        failedHandshakes = 0
        onStatus('online')
      }
      current.onmessage = (message) => {
        if (socket !== current) return
        try { accept(JSON.parse(String(message.data)) as StudioEvent) }
        catch { disconnect(true) }
      }
      current.onclose = () => disconnect(false)
      current.onerror = () => disconnect(true)
    } catch (error) {
      if (disposed) return
      if (error instanceof StudioApiError && error.status === 410) {
        stop('deleted')
        return
      }
      if (error instanceof StudioApiError && error.status === 412 && transport.verifyWorkflow) {
        stop('storage-mismatch')
        return
      }
      missingRoutes = error instanceof StudioApiError && error.status === 404 ? missingRoutes + 1 : 0
      if (transport.verifyWorkflow && missingRoutes >= 2) {
        missingRoutes = 0
        const valid = await verifyWorkflow()
        if (disposed) return
        if (valid !== undefined) { stop(valid === true ? 'unavailable' : valid === false ? 'workflow-mismatch' : valid); return }
      }
      reconnect()
    }
  }
  onStatus('connecting')
  void connect()
  return dispose
}

async function studioHealth(signal?: AbortSignal): Promise<StudioHealth> {
  if (signal?.aborted) throw signal.reason
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  signal?.addEventListener('abort', abort, { once: true })
  // Give all retries and the response body one deadline for the connection screen.
  const timer = setTimeout(() => {
    controller.abort(new DOMException("连接工作室超时。", 'TimeoutError'))
  }, healthWaitMilliseconds)
  try {
    return await request<StudioHealth>('/api/v1/health', { signal: controller.signal, cache: 'no-store' })
  } catch (error) {
    if (controller.signal.aborted) throw controller.signal.reason
    throw error
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}

function bindStudioStorage(storageId: string): StudioClient {
  if (!isStudioStorageId(storageId)) throw new Error("未能确认 StateWeaver 存储身份。")
  const endpoint = `/api/v1/studio/v2/stores/${storageId}/projects`
  const projectEndpoint = (id: string) => `${endpoint}/${encodeURIComponent(id)}`
  return {
    mediaUrl: (url) => mediaUrl(url, endpoint),
    productionStatus: (id, details, signal) => request<ProductionStatus>(`${projectEndpoint(id)}/production-status?details=${details}`, { signal }),
    setPreview: (id, setId, options, signal) => request<SetPreview>(`${projectEndpoint(id)}/sets/${encodeURIComponent(setId)}/preview?${new URLSearchParams({ prepare: String(options.prepare ?? false), retry: String(options.retry ?? false) })}`, { signal }),
    setDependencies: (id, setId, signal) => request<SetDependencyReport>(`${projectEndpoint(id)}/sets/${encodeURIComponent(setId)}/dependencies`, { signal }),
    readiness: (id, clipId, signal) => request<ClipReadiness>(`${projectEndpoint(id)}/clips/${encodeURIComponent(clipId)}/readiness`, { signal }),
    comparison: (id, clipId, after, signal) => request<CandidateComparison>(`${projectEndpoint(id)}/clips/${encodeURIComponent(clipId)}/comparison${after ? `?after=${encodeURIComponent(after)}` : ''}`, { signal }),
    memory: (id, scope, status, after, signal) => request<EntityPage>(`${projectEndpoint(id)}/memory?scope=${encodeURIComponent(scope)}&status=${status}&limit=50${after ? `&after=${encodeURIComponent(after)}` : ''}`, { signal }),
    entityLabels: (id, ids, signal) => request<EntityLabelBatch>(`${projectEndpoint(id)}/entity-labels?${new URLSearchParams(ids.map((value) => ['ids', value]))}`, { signal }),
    layout: (id, signal) => request<WorkspaceLayout>(`${projectEndpoint(id)}/layout`, { signal }),
    saveLayout: (id, input, signal) => request<WorkspaceLayout>(`${projectEndpoint(id)}/layout`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input), signal }),
    configurePreparation: (id, clipId, input) => request<CommandResult>(`${projectEndpoint(id)}/clips/${encodeURIComponent(clipId)}/preparation`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) }),
    list: (after, signal) => request<ProjectList>(`${endpoint}?limit=24${after ? `&after=${encodeURIComponent(after)}` : ''}`, { signal }),
    create: (input) => post<StudioSnapshot>(endpoint, input),
    deleteProject: (id, input) => request<CommandResult>(projectEndpoint(id), {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
    }),
    get: (id, signal) => request<StudioSnapshot>(projectEndpoint(id), { signal }),
    details: (id, scope, signal) => request<StudioSnapshot>(`${projectEndpoint(id)}/details?scope=${encodeURIComponent(scope)}&compact=true`, { signal }),
    transcript: (id, actor, before, signal) => request<TranscriptPage>(`${projectEndpoint(id)}/actors/${encodeURIComponent(actor)}/transcript?limit=50${before === undefined ? '' : `&before=${before}`}`, { signal }),
    body: (id, ref, offset = 0, signal) => request<BodyChunk>(`${projectEndpoint(id)}/bodies/${encodeURIComponent(ref)}?offset=${offset}&limit=65536`, { signal }),
    history: (id, kind, parentId, after, signal) => request<EntityPage>(`${projectEndpoint(id)}/history?kind=${kind}&parent_id=${encodeURIComponent(parentId)}&limit=50${after ? `&after=${encodeURIComponent(after)}` : ''}`, { signal }),
    action: (id, path, input) => post<CommandResult>(`${projectEndpoint(id)}/${path}`, input),
    subscribe: (id, onEvent, onStatus, cursor) => subscribeToProject(id, onEvent, onStatus, {
      cursor,
      verifyWorkflow: async (signal) => studioRouteMismatch(await studioHealth(signal), storageId) ?? true,
      batch: (after) => request<EventBatch>(`${projectEndpoint(id)}/events?after=${after}`, { cache: 'no-store' }),
      socket: (after) => {
        const url = new URL(`${projectEndpoint(id)}/events/ws?after=${after}`, window.location.href)
        url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
        return new WebSocket(url)
      },
    }),
  }
}

export const studioConnection: StudioConnection = { health: studioHealth, bindStorage: bindStudioStorage }
