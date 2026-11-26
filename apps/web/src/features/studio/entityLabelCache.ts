import type { EntityLabelBatch } from './model'

export type EntityLabelLoader = (ids: string[], signal?: AbortSignal) => Promise<EntityLabelBatch>
type Label = EntityLabelBatch['labels'][string]
export type LabelSnapshot = { status: 'idle' | 'loading' } | { status: 'ready'; value: Label } | { status: 'missing' | 'error' }
type CachedLabel = { snapshot: LabelSnapshot; resolvedAt: number }
type Batch = { ids: string[]; controller: AbortController; generation: number }
const idle: LabelSnapshot = Object.freeze({ status: 'idle' })
const loading: LabelSnapshot = Object.freeze({ status: 'loading' })
const RETRY_AFTER_MS = 30_000
const BATCH_DELAY_MS = 16

/** One project owns a bounded name cache, shared by all its visible link providers. */
export class EntityLabelCache {
  private records = new Map<string, CachedLabel>()
  private listeners = new Map<string, Set<() => void>>()
  private visible = new Map<string, number>()
  private queued = new Set<string>()
  private activeIds = new Set<string>()
  private batches = new Set<Batch>()
  private timer?: ReturnType<typeof setTimeout>
  private owners = 0
  private generation = 0

  constructor(private load: EntityLabelLoader, private capacity = 2048) {}

  get size() { return this.records.size }
  getSnapshot = (id: string): LabelSnapshot => this.records.get(id)?.snapshot ?? (this.queued.has(id) || this.activeIds.has(id) ? loading : idle)

  subscribe(id: string, listener: () => void) {
    const listeners = this.listeners.get(id) ?? new Set()
    listeners.add(listener); this.listeners.set(id, listeners)
    return () => { listeners.delete(listener); if (!listeners.size) this.listeners.delete(id) }
  }

  /** Delay final teardown by a microtask so React's development effect replay can reattach. */
  attach() {
    this.owners += 1
    this.schedule()
    return () => {
      this.owners = Math.max(0, this.owners - 1)
      void Promise.resolve().then(() => { if (!this.owners) this.stop() })
    }
  }

  watch(id: string) {
    this.visible.set(id, (this.visible.get(id) ?? 0) + 1)
    const cached = this.records.get(id)
    if (cached && (cached.snapshot.status === 'ready' || Date.now() - cached.resolvedAt < RETRY_AFTER_MS)) {
      this.records.delete(id); this.records.set(id, cached)
    } else if (!this.activeIds.has(id)) {
      this.records.delete(id)
      this.queued.add(id)
      this.emit(id)
      this.schedule()
    }
    return () => {
      const remaining = (this.visible.get(id) ?? 1) - 1
      if (remaining > 0) this.visible.set(id, remaining)
      else { this.visible.delete(id); if (this.queued.delete(id)) this.emit(id) }
    }
  }

  private emit(id: string) { for (const listener of this.listeners.get(id) ?? []) listener() }

  private schedule() {
    if (!this.owners || this.timer !== undefined || this.batches.size >= 2 || !this.queued.size) return
    this.timer = setTimeout(() => { this.timer = undefined; this.flush() }, BATCH_DELAY_MS)
  }

  private store(id: string, snapshot: LabelSnapshot) {
    this.records.delete(id)
    this.records.set(id, { snapshot, resolvedAt: Date.now() })
    while (this.records.size > this.capacity) {
      const oldest = [...this.records.keys()].find((key) => !this.visible.has(key)) ?? this.records.keys().next().value
      if (oldest === undefined) break
      this.records.delete(oldest); this.emit(oldest)
    }
    this.emit(id)
  }

  private flush() {
    if (!this.owners) return
    while (this.batches.size < 2 && this.queued.size) {
      const ids = [...this.queued].filter((id) => this.visible.has(id)).slice(0, 64)
      if (!ids.length) { this.queued.clear(); return }
      const batch: Batch = { ids, controller: new AbortController(), generation: this.generation }
      for (const id of ids) { this.queued.delete(id); this.activeIds.add(id) }
      this.batches.add(batch)
      void Promise.resolve().then(() => {
        if (batch.controller.signal.aborted) return undefined
        return this.load(ids, batch.controller.signal)
      }).then((result) => {
        if (batch.controller.signal.aborted || batch.generation !== this.generation) return
        if (!result) throw new Error('Empty entity label response')
        const missing = new Set(result.missing_ids)
        for (const id of ids) {
          const label = Object.hasOwn(result.labels, id) ? result.labels[id] : undefined
          this.store(id, label?.label?.trim() ? { status: 'ready', value: { label: label.label.slice(0, 400), description: label.description.slice(0, 600), kind: label.kind } }
            : { status: missing.has(id) ? 'missing' : 'error' })
        }
      }).catch(() => {
        if (!batch.controller.signal.aborted && batch.generation === this.generation) for (const id of ids) this.store(id, { status: 'error' })
      }).finally(() => {
        if (batch.generation !== this.generation) return
        this.batches.delete(batch)
        for (const id of ids) { this.activeIds.delete(id); this.emit(id) }
        this.schedule()
      })
    }
  }

  private stop() {
    this.generation += 1
    if (this.timer !== undefined) clearTimeout(this.timer)
    this.timer = undefined
    for (const batch of this.batches) batch.controller.abort()
    this.batches.clear(); this.activeIds.clear(); this.queued.clear()
  }
}

export const emptyLabelSnapshot = () => idle
