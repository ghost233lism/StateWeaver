import { tr } from "../../ui/locale"
import { useMemo, useSyncExternalStore } from 'react'
import type { Viewport } from '@xyflow/react'
import { StudioApiError, type StudioClient } from './client'
import type { WorkspaceLayout, WorkspaceLayoutUpdate } from './model'

type LayoutNodes = NonNullable<WorkspaceLayout['nodes']>
type Patch = Pick<WorkspaceLayoutUpdate, 'nodes' | 'asset_order'>
export type LayoutStatus = 'loading' | 'saved' | 'dirty' | 'saving' | 'error' | 'conflict' | 'local'
const emptyLayout = (): WorkspaceLayout => ({ version: 0, nodes: {}, asset_order: {} })
const hasPatch = (patch: Patch) => Object.keys(patch.nodes ?? {}).length > 0 || Object.keys(patch.asset_order ?? {}).length > 0
export function mergeLayout(layout: WorkspaceLayout, patch: Patch): WorkspaceLayout {
  return { version: layout.version, nodes: { ...layout.nodes, ...patch.nodes }, asset_order: { ...layout.asset_order, ...patch.asset_order } }
}

export function readViewport(projectId: string): Viewport | undefined {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(`studio:viewport:${projectId}`) ?? 'null')
    if (!value || typeof value !== 'object') return
    const view = value as Viewport
    if ([view.x, view.y, view.zoom].every(Number.isFinite) && view.zoom >= 0.15 && view.zoom <= 2) return view
  } catch { /* Browser preferences may be unavailable. */ }
}
export function writeViewport(projectId: string, viewport: Viewport) {
  try { localStorage.setItem(`studio:viewport:${projectId}`, JSON.stringify(viewport)) } catch { /* Layout saving still works without browser storage. */ }
}

export function readCanvasGrid(projectId: string): boolean {
  try { return localStorage.getItem(`studio:grid:${projectId}`) === 'true' } catch { return false }
}
export function writeCanvasGrid(projectId: string, visible: boolean) {
  try { localStorage.setItem(`studio:grid:${projectId}`, String(visible)) } catch { /* Browser preferences are optional. */ }
}

type LayoutClient = Pick<StudioClient, 'layout' | 'saveLayout'>
type LayoutSnapshot = { layout: WorkspaceLayout; status: LayoutStatus; message: string; resetPending: boolean }

/** A project queue outlives its mounted canvas, including unsaved conflict drafts. */
class WorkspaceController {
  private base = emptyLayout()
  private pending: Patch = {}
  private inFlight: Patch = {}
  private status: LayoutStatus = 'loading'
  private message = ''
  private resetPending = false
  private initialized = false
  private writing = false
  private readController?: AbortController
  private timer?: ReturnType<typeof setTimeout>
  private listeners = new Set<() => void>()
  private failedOperation: 'save' | 'load' = 'load'
  private loadKeepsChanges = false
  private snapshot: LayoutSnapshot = { layout: emptyLayout(), status: 'loading', message: '', resetPending: false }

  constructor(private projectId: string, private client?: LayoutClient) {}
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => {
    const firstSubscriber = this.listeners.size === 0
    this.listeners.add(listener)
    if (!this.initialized || firstSubscriber && this.status === 'saved') {
      this.initialized = true
      void this.load(true)
    }
    return () => {
      this.listeners.delete(listener)
      if (!this.listeners.size) {
        clearTimeout(this.timer)
        // In-flight completion drains later edits against the returned version.
        if (this.status === 'dirty') void this.save()
      }
    }
  }
  private publish() {
    this.snapshot = { layout: mergeLayout(mergeLayout(this.base, this.inFlight), this.pending),
      status: this.status, message: this.message, resetPending: this.resetPending }
    this.listeners.forEach((listener) => listener())
  }
  private schedule() {
    clearTimeout(this.timer)
    if (!this.listeners.size) { void this.save(); return }
    this.timer = setTimeout(() => { void this.save() }, 450)
  }
  private async load(keepChanges: boolean) {
    if (this.writing) return
    if (!this.client?.layout || !this.client.saveLayout) { this.status = 'local'; this.publish(); return }
    clearTimeout(this.timer)
    this.readController?.abort()
    const controller = new AbortController()
    this.readController = controller
    this.loadKeepsChanges = keepChanges
    this.failedOperation = 'load'
    this.status = 'loading'; this.message = ''; this.publish()
    try {
      const layout = await this.client.layout(this.projectId, controller.signal)
      if (controller.signal.aborted) return
      this.base = layout
      if (!keepChanges) { this.pending = {}; this.resetPending = false }
      this.status = hasPatch(this.pending) || this.resetPending ? 'dirty' : 'saved'
      this.publish()
      if (this.status === 'dirty') this.schedule()
    } catch (error) {
      if (controller.signal.aborted) return
      this.status = 'error'; this.message = error instanceof Error ? error.message : tr("无法载入布局"); this.publish()
    }
  }
  private async save() {
    if (this.writing || this.status !== 'dirty' || !this.client?.saveLayout || (!hasPatch(this.pending) && !this.resetPending)) return
    const sent = this.pending
    const reset = this.resetPending
    this.pending = {}; this.inFlight = sent; this.writing = true
    this.status = 'saving'; this.message = ''; this.failedOperation = 'save'; this.publish()
    const controller = new AbortController()
    try {
      const layout = await this.client.saveLayout(this.projectId, {
        expected_version: this.base.version, ...sent, reset,
      }, controller.signal)
      this.base = layout; this.inFlight = {}; this.resetPending = false
      this.status = hasPatch(this.pending) ? 'dirty' : 'saved'
    } catch (error) {
      this.inFlight = {}
      this.pending = { nodes: { ...sent.nodes, ...this.pending.nodes }, asset_order: { ...sent.asset_order, ...this.pending.asset_order } }
      this.status = error instanceof StudioApiError && error.status === 409 ? 'conflict' : 'error'
      this.message = this.status === 'conflict' ? tr("其他窗口已更新布局。载入共享布局，或确认合并本窗口的调整。") : error instanceof Error ? error.message : tr("布局未保存，请重试。")
    } finally {
      this.writing = false
      this.publish()
      if (this.status === 'dirty') this.schedule()
    }
  }
  private patch(update: Patch) {
    if (this.status === 'loading') return
    if (this.status === 'local') { this.base = mergeLayout(this.base, update); this.publish(); return }
    this.pending = { nodes: { ...this.pending.nodes, ...update.nodes }, asset_order: { ...this.pending.asset_order, ...update.asset_order } }
    if (!['saving', 'error', 'conflict'].includes(this.status)) { this.status = 'dirty'; this.schedule() }
    this.publish()
  }
  updateNodes = (nodes: LayoutNodes) => this.patch({ nodes })
  updateAssetOrder = (kind: string, ids: string[]) => this.patch({ asset_order: { [kind]: ids } })
  reset = () => {
    if (['saving', 'loading', 'conflict'].includes(this.status)) return
    clearTimeout(this.timer)
    this.pending = {}
    if (this.status === 'local') { this.base = emptyLayout(); this.publish(); return }
    this.resetPending = true; this.status = 'dirty'; this.publish(); void this.save()
  }
  retry = () => {
    if (this.status !== 'error') return
    if (this.failedOperation === 'load') { void this.load(this.loadKeepsChanges); return }
    this.status = 'dirty'; void this.save()
  }
  reload = () => { void this.load(false) }
  reapply = () => { void this.load(true) }
}

const localClient: LayoutClient = {}
const controllers = new WeakMap<LayoutClient, Map<string, WorkspaceController>>()
function projectController(projectId: string, client: LayoutClient = localClient) {
  let projects = controllers.get(client)
  if (!projects) { projects = new Map(); controllers.set(client, projects) }
  let controller = projects.get(projectId)
  if (!controller) { controller = new WorkspaceController(projectId, client); projects.set(projectId, controller) }
  return controller
}

/** Serializes project writes independently of canvas navigation or inspector state. */
export function useWorkspaceLayout(projectId: string, client?: LayoutClient) {
  const controller = useMemo(() => projectController(projectId, client), [projectId, client])
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  return { ...snapshot, updateNodes: controller.updateNodes, updateAssetOrder: controller.updateAssetOrder,
    reset: controller.reset, retry: controller.retry, reload: controller.reload, reapply: controller.reapply }
}
