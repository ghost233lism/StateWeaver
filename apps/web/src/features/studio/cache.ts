import type { StudioEvent, StudioProject, StudioSnapshot, TranscriptItem } from './model'

const collections = ['actors', 'sets', 'clips', 'assets', 'states', 'references', 'candidates', 'proposals', 'jobs', 'media', 'transcript', 'story_events', 'reference_plans', 'visual_tasks'] as const
export type ProjectCache = {
  project: StudioProject; cursor: number; versions: Record<string, number>; counts: Record<string, number>;
  snapshotCursor: number; coreVersions: Record<string, number>;
}
const historyCollections = new Set<string>(['states', 'references', 'candidates', 'media', 'jobs', 'proposals', 'reference_plans', 'visual_tasks'])
const scopedCoreFields = new Set(['screenplay', 'global_memory', 'export_manifest'])
const projectedFields = new Set([
  ...collections, ...scopedCoreFields, 'revision', 'context_reads', 'image_quarantines', 'external_reviews',
  'delegate_tasks', 'executive_inbox', 'executive_documents', 'executive_document_versions',
])
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

function coreVersion(cache: ProjectCache | null | undefined, field: string): number {
  const snapshotVersion = scopedCoreFields.has(field) ? -1 : cache?.snapshotCursor ?? -1
  return Math.max(snapshotVersion, cache?.coreVersions[field] ?? -1)
}

export function isCurrentMemoryProjection(project: StudioProject, incoming: StudioSnapshot, cache?: ProjectCache | null) {
  return incoming.cursor >= coreVersion(cache, 'global_memory') && (incoming.project.global_memory?.revision ?? 0) >= (project.global_memory?.revision ?? 0)
}

export function mergeFinalProjection(project: StudioProject, incoming: StudioSnapshot, cache?: ProjectCache | null) {
  const incomingId = incoming.project.final_media_id
  const final_media_id = incoming.cursor >= coreVersion(cache, 'final_media_id') && incoming.project.revision >= project.revision
    ? incomingId : project.final_media_id
  if (final_media_id !== incomingId || incoming.cursor < coreVersion(cache, 'export_manifest')) return { final_media_id }
  return { final_media_id, export_manifest: incoming.project.export_manifest }
}

/** Entity versions fence a late detail response, even when business revisions match. */
export function compactCache(cache: ProjectCache): ProjectCache {
  const project = { ...cache.project }
  const versions = { ...cache.versions }
  for (const collection of ['transcript', 'states', 'references', 'candidates', 'media', 'jobs', 'proposals', 'reference_plans', 'visual_tasks'] as const) {
    const entries = Object.entries(project[collection] ?? {})
    const limit = collection === 'transcript' ? 200 : 500
    if (entries.length <= limit) continue
    const kept = entries.slice(-limit)
    project[collection] = Object.fromEntries(kept) as never
    for (const [id] of entries.slice(0, -limit)) delete versions[`${collection}:${id}`]
  }
  return { ...cache, project, versions }
}

export function mergeSnapshot(current: ProjectCache | null, incoming: StudioSnapshot): ProjectCache {
  const scope = incoming.scope ?? 'overview'
  if (!current || current.project.id !== incoming.project.id) return {
    project: incoming.project, cursor: incoming.cursor, versions: incoming.entity_versions ?? {}, counts: incoming.counts ?? {},
    snapshotCursor: incoming.cursor, coreVersions: { screenplay: incoming.cursor, global_memory: incoming.cursor, ...(scope === 'final' && { export_manifest: incoming.cursor }) },
  }
  const core = Object.fromEntries(Object.entries(incoming.project).filter(([field]) => !projectedFields.has(field) && incoming.cursor >= coreVersion(current, field)))
  let project = { ...current.project, ...core, revision: Math.max(current.project.revision, incoming.project.revision) }
  const coreVersions = { ...current.coreVersions }
  if (scope === 'memory:global' && isCurrentMemoryProjection(project, incoming, current)) {
    project.global_memory = incoming.project.global_memory
    coreVersions.global_memory = incoming.cursor
  }
  const fullScreenplay = scope === 'screenplay' || scope === 'final' || scope === 'overview' && incoming.project.status === 'screenwriting'
  if (fullScreenplay && incoming.cursor >= coreVersion(current, 'screenplay') && incoming.project.revision >= current.project.revision) {
    project.screenplay = incoming.project.screenplay
    coreVersions.screenplay = incoming.cursor
  }
  if (scope === 'final') {
    const final = mergeFinalProjection(project, incoming, current)
    if ('export_manifest' in final) {
      project.export_manifest = final.export_manifest
      coreVersions.export_manifest = incoming.cursor
    }
  }
  const versions = { ...current.versions }
  for (const collection of collections) {
    const accepted: Record<string, unknown> = {}
    for (const [id, value] of Object.entries(incoming.project[collection] ?? {})) {
      const key = `${collection}:${id}`
      const version = incoming.entity_versions?.[key] ?? incoming.cursor
      if (version < (versions[key] ?? -1)) {
        const previous = record(project[collection])[id]
        const hydrated = hydrateOlderJobSubmission(collection, previous, value, scope)
        if (hydrated !== previous) accepted[id] = hydrated
        continue
      }
      accepted[id] = mergeEntityProjection(collection, record(project[collection])[id], value, scope); versions[key] = version
    }
    if (Object.keys(accepted).length) {
      const previous = { ...record(project[collection]) }; for (const id of Object.keys(accepted)) delete previous[id]
      project = { ...project, [collection]: { ...previous, ...accepted } }
    }
  }
  // The scoped read carries current project core but does not replace event replay.
  return compactCache({
    project, cursor: current.cursor, versions, coreVersions, snapshotCursor: Math.max(current.snapshotCursor, incoming.cursor),
    counts: incoming.cursor >= current.snapshotCursor ? { ...current.counts, ...incoming.counts } : current.counts,
  })
}

/** Only bounded changes enter the live canvas cache. History pages remain local to open panels. */
export function applyProjectEvent(current: ProjectCache, event: StudioEvent): ProjectCache {
  if (event.project_id !== current.project.id) return current
  const data = event.data ?? {}
  const changesToCore = Object.entries(record(data.core)).filter(([field]) => field !== 'revision' && event.cursor > coreVersion(current, field))
  const core = Object.fromEntries(changesToCore)
  const coreVersions = changesToCore.length ? { ...current.coreVersions } : current.coreVersions
  for (const [field] of changesToCore) coreVersions[field] = event.cursor
  const revision = typeof data.revision === 'number' ? data.revision : 0
  let project = { ...current.project, ...core, revision: Math.max(current.project.revision, revision) } as StudioProject
  // Event core carries metadata; full screenplay and story entries come from their scopes.
  if (core.screenplay) project.screenplay = { ...current.project.screenplay, ...record(core.screenplay), text: current.project.screenplay?.text ?? '', facts: current.project.screenplay?.facts ?? [], manuscript: current.project.screenplay?.manuscript ?? null } as StudioProject['screenplay']
  if (core.global_memory) project.global_memory = { ...current.project.global_memory, ...record(core.global_memory), story: current.project.global_memory?.story ?? [], asset_state_ids: current.project.global_memory?.asset_state_ids ?? [] } as StudioProject['global_memory']
  const versions = { ...current.versions }
  const changes = { ...record(data.changes) }
  const eventVersions = { ...record(data.versions) }
  if (event.kind === 'transcript.delta' && typeof data.item_id === 'string') {
    const previous = project.transcript?.[data.item_id]
    const preview = data.item as TranscriptItem | undefined
    const version = typeof data.version === 'number' ? data.version : event.cursor
    if (preview && version > (previous?.version ?? -1)) {
      const previousCharacters = Array.from(previous?.text ?? '')
      const previousLength = previousCharacters.length
      const contiguous = previous && (data.offset === undefined || data.offset === previousLength) && (previous.text_length == null || previous.text_length === previousLength)
      const characters = contiguous && typeof data.delta === 'string' ? [...previousCharacters, ...Array.from(data.delta)] : Array.from(preview.text)
      changes.transcript = { ...record(changes.transcript), [data.item_id]: { ...preview, text: characters.slice(0, 65_536).join(''), text_length: typeof data.text_length === 'number' ? data.text_length : preview.text_length, version } }
      eventVersions[`transcript:${data.item_id}`] = version
    }
  }
  if (event.kind === 'transcript.upsert' && data.item) {
    const item = data.item as TranscriptItem
    changes.transcript = { ...record(changes.transcript), [item.id]: item }
  }
  for (const collection of collections) {
    const updates = record(changes[collection])
    const removed = record(data.removed)[collection]
    if (!Object.keys(updates).length && !Array.isArray(removed)) continue
    const next = { ...record(project[collection]) }
    for (const [id, payload] of Object.entries(updates)) {
      if ((historyCollections.has(collection) || collection === 'actors' && record(payload).role === 'visual_analyst') && !(id in next)) continue
      const key = `${collection}:${id}`
      const version = Number(eventVersions[key] ?? event.cursor)
      if (version <= (versions[key] ?? -1)) continue
      next[id] = mergeEntityProjection(collection, next[id], payload); versions[key] = version
    }
    for (const id of Array.isArray(removed) ? removed : []) {
      if (typeof id !== 'string') continue
      const key = `${collection}:${id}`
      if (!(id in next) && !(key in versions)) continue
      const version = Number(eventVersions[key] ?? event.cursor)
      if (version < (versions[key] ?? -1)) continue
      delete next[id]; versions[key] = version // Tombstones prevent stale detail resurrection.
    }
    if (collection === 'transcript') {
      const items = Object.values(next) as TranscriptItem[]
      items.sort((a, b) => (b.sequence ?? 0) - (a.sequence ?? 0))
      for (const item of items.slice(200)) delete versions[`transcript:${item.id}`]
      project = { ...project, transcript: Object.fromEntries(items.slice(0, 200).map((item) => [item.id, item])) }
    } else project = { ...project, [collection]: next }
  }
  return { ...current, project, cursor: Math.max(current.cursor, event.cursor), versions, coreVersions }
}

export function mergeTranscript(current: TranscriptItem[], incoming: TranscriptItem[]): TranscriptItem[] {
  const byId = new Map(current.map((item) => [item.id, item]))
  for (const item of incoming) if ((item.version ?? 0) >= (byId.get(item.id)?.version ?? -1)) byId.set(item.id, item)
  return [...byId.values()].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0) || (a.created_at ?? '').localeCompare(b.created_at ?? ''))
}

/** Omitted detail fields in a summary are placeholders, not authoritative deletions. */
export function mergeEntityProjection(collection: string, current: unknown, incoming: unknown, scope = 'event'): unknown {
  const previous = record(current)
  const next = record(incoming)
  if (!current) return incoming
  const id = String(next.id ?? previous.id ?? '')
  if (collection === 'sets' && scope !== `memory:${id}`) return { ...next, memory: { ...record(next.memory), story: record(previous.memory).story ?? [] } }
  const fullCandidate = scope === `candidate:${id}` || scope === `clip:${next.clip_id}`
  if (collection === 'candidates' && !fullCandidate) return { ...next, prompt: previous.prompt ?? next.prompt, analysis: { ...record(previous.analysis), ...record(next.analysis) }, review_batches: next.review_batches ?? previous.review_batches }
  if (collection === 'clips' && scope !== `clip:${id}`) return { ...next, preparation_history: next.preparation_history ?? previous.preparation_history }
  if (collection === 'references' && scope !== `reference:${id}`) return { ...next, prompt: previous.prompt ?? next.prompt }
  if (collection === 'jobs' && !fullJobScope(next, scope)) return {
    ...next, request: { ...record(previous.request), ...record(next.request) },
    result: withSubmissionProvenance(record(next.result), record(previous.result)),
  }
  if (collection === 'reference_plans' && scope !== `reference-plan:${id}`) return {
    ...previous, ...next, prompt: next.prompt || previous.prompt,
    reference_inputs: previous.reference_inputs ?? next.reference_inputs,
    video_inputs: previous.video_inputs ?? next.video_inputs, audio_inputs: previous.audio_inputs ?? next.audio_inputs,
  }
  return incoming
}

function fullJobScope(job: Record<string, unknown>, scope: string) {
  return scope === `job:${job.id}` || scope === `candidate:${job.target_id}` || scope.startsWith('clip:')
}

function withSubmissionProvenance(current: Record<string, unknown>, older: Record<string, unknown>) {
  if (!Object.keys(record(older.provenance)).length) return current
  return { ...current, provenance: { ...record(older.provenance), ...record(current.provenance) } }
}

/** A poll changes job status/version, never the same job's submitted creative inputs. */
export function hydrateOlderJobSubmission(collection: string, current: unknown, incoming: unknown, scope: string): unknown {
  const latest = record(current), detail = record(incoming)
  // Never resurrect a tombstone, borrow another task, or hydrate from a summary.
  if (collection !== 'jobs' || !current || !fullJobScope(detail, scope) || latest.id !== detail.id || latest.kind !== detail.kind || latest.target_id !== detail.target_id) return current
  if (!Object.keys(record(detail.request)).length && !Object.keys(record(record(detail.result).provenance)).length) return current
  return {
    ...latest, request: { ...record(detail.request), ...record(latest.request) },
    result: withSubmissionProvenance(record(latest.result), record(detail.result)),
  }
}
