import { tr } from "../../ui/locale"
import { activityFor } from './activity'
import { type StudioProject, type TranscriptItem } from './model'

type Fields = Record<string, unknown>
const object = (value: unknown): Fields => value && typeof value === 'object' && !Array.isArray(value) ? value as Fields : {}
const text = (value: unknown) => typeof value === 'string' ? value : ''
const revision = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined
const ids = (value: unknown) => Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string' && Boolean(id)).slice(0, 64) : []
const memoryKinds: Record<string, string> = { get constraint() { return tr("已确认事实") }, get plan() { return tr("制作计划") }, get observation() { return tr("实际观察") } }
const actions = new Set(['write_memory', 'correct_memory_scope', 'correct_state_scope', 'create_asset', 'create_state', 'generate_reference', 'select_reference'])

export type OperationChange = {
  action: string
  scope: string
  category: string
  summary: string
  revisionBefore?: number
  revisionAfter?: number
  bindings?: { before?: string[]; after: string[]; countBefore?: number; countAfter: number; truncated: boolean }
  reference?: { id: string; beforeKnown: boolean; beforeId: string | null; parentId: string; stateId: string; assetId: string; view: string }
}

/** Applied business records carry historical facts; native receipts and failed calls do not. */
export function operationChangeFor(item: TranscriptItem, project: StudioProject): OperationChange | null {
  const activity = activityFor(item)
  const { fields, name } = activity
  if (activity.status !== 'complete' || !actions.has(name) || fields.type === 'mcpToolCall'
    || !['memory_change', 'asset_change'].includes(item.kind)) return null
  const actor = project.actors?.[item.actor_id]
  const setId = text(fields.set_id) || (actor?.role === 'set_director' ? actor.set_id ?? '' : '')
  const isSet = fields.scope !== 'global' && (fields.scope === 'set' || Boolean(setId))
  const scope = isSet ? tr("片场记忆{{0}}", project.sets?.[setId]?.title ? ` · ${project.sets[setId].title}` : '')
    : fields.scope === 'global' || actor?.role === 'executive' ? tr("全局记忆") : tr("记忆")
  const entry = object(fields.entry)
  const revisionBefore = revision(fields.memory_revision_before) ?? (name === 'write_memory' ? revision(fields.memory_revision) : undefined)
  const revisionAfter = revision(fields.memory_revision_after) ?? (name === 'write_memory' && revisionBefore !== undefined ? revisionBefore + 1 : undefined)
  const summary = (name === 'correct_memory_scope' ? text(fields.reason) : '') || text(fields.summary) || text(entry.text) || text(fields.description)
    || (Array.isArray(fields.entries) ? fields.entries.filter((entry): entry is string => typeof entry === 'string').join('\n') : '')
    || activity.description
  const change: OperationChange = { action: name, scope,
    category: name === 'correct_memory_scope' ? tr("剧情记忆 · 适用范围") : name === 'write_memory' ? tr("剧情记忆{{0}}", memoryKinds[text(fields.entry_kind) || text(entry.kind)] ? ` · ${memoryKinds[text(fields.entry_kind) || text(entry.kind)]}` : '') : tr("视觉资产"),
    summary, revisionBefore, revisionAfter,
  }
  if (name === 'write_memory' && Array.isArray(fields.asset_state_ids)) {
    const after = ids(fields.asset_state_ids)
    const before = Array.isArray(fields.before_asset_state_ids) ? ids(fields.before_asset_state_ids) : undefined
    change.bindings = { after, before, countBefore: revision(fields.asset_state_count_before) ?? before?.length,
      countAfter: revision(fields.asset_state_count_after) ?? after.length, truncated: fields.asset_state_ids_truncated === true }
  }
  if (name === 'generate_reference' || name === 'select_reference') {
    const beforeKnown = fields.before_reference_id === null || Boolean(text(fields.before_reference_id))
    change.reference = { id: text(fields.reference_id) || text(fields.revision_id), beforeKnown,
      beforeId: beforeKnown ? text(fields.before_reference_id) || null : null,
      parentId: text(fields.parent_id), stateId: text(fields.state_id), assetId: text(fields.asset_id), view: text(fields.view) }
  }
  return change
}
