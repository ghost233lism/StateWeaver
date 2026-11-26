import { tr } from "../../ui/locale"
import { actionLabels } from './DetailValue'
import type { Selection, StudioProject, TranscriptItem } from './model'

type Fields = Record<string, unknown>
const reads = new Set(['get_project', 'get_history', 'get_detail', 'get_body', 'read_media', 'get_required_context', 'query_memory', 'batch_get_details', 'batch_read_media', 'get_clip_readiness', 'get_reference_candidates', 'compare_candidates', 'get_operation_receipt'])
const operationKinds = new Set(['tool', 'memory_change', 'asset_change'])
const object = (value: unknown): Fields => value && typeof value === 'object' && !Array.isArray(value) ? value as Fields : {}
const string = (value: unknown): string => typeof value === 'string' ? value : ''

function argumentsOf(value: unknown): Fields {
  if (typeof value !== 'string') return object(value)
  if (value.length > 32_768) return {}
  try { return object(JSON.parse(value)) } catch { return {} }
}

/** Read only bounded inline metadata; archived bodies are never fetched for this view. */
export function activityFor(item: TranscriptItem) {
  const data = object(item.data)
  const activity = { ...object(data.activity), ...object(item.activity) }
  const args = argumentsOf(data.arguments)
  const payload = object(args.payload)
  const rawName = string(activity.action) || string(data.action) || string(args.action) || string(data.tool) || (actionLabels[item.text] ? item.text : '') || string(data.name) || item.text
  const name = rawName.split('__').at(-1) ?? rawName
  const fields = { ...data, ...args, ...payload, ...activity }
  const result = object(data.result)
  const failed = item.status === 'failed' || ['failed', 'error', 'cancelled'].includes(string(activity.status) || string(data.status))
    || data.isError === true || result.isError === true || Boolean(data.error) || Boolean(activity.error)
  const status = failed ? 'failed' : item.status
  const title = actionLabels[name] ?? (item.kind === 'memory_change' ? tr("记忆更新") : item.kind === 'asset_change' ? tr("资产更新") : name || tr("执行操作"))
  const description = string(fields.summary) || (name === 'write_memory' ? string(object(fields.entry).text) : '') || string(fields.reason)
    || (item.text !== rawName && !actionLabels[item.text] && item.text !== string(data.tool) ? item.text : '')
  const error = string(activity.error) || string(data.error) || string(object(data.error).message)
  return { name, title, description, fields, status, error, read: operationKinds.has(item.kind) && reads.has(name) && status === 'complete' }
}

export type ActivityRow = { kind: 'item'; item: TranscriptItem; native?: TranscriptItem[] } | { kind: 'reads'; items: TranscriptItem[] }

/** Coalesce reads only across adjacent successful records. Prose and errors are barriers. */
export function activityRows(items: TranscriptItem[]): ActivityRow[] {
  const byId = new Map(items.map((item) => [item.id, item]))
  const nativeByBusiness = new Map<string, TranscriptItem[]>()
  const hidden = new Set<string>()
  for (const item of items) {
    const data = object(item.data)
    if ((object(item.activity ?? data.activity).type ?? data.type) !== 'mcpToolCall' || activityFor(item).status !== 'complete') continue
    const fields = object(item.activity ?? data.activity)
    const receipt = object(object(data.result).structuredContent)
    const batchIds = fields.item_ids ?? receipt.item_ids
    if (activityFor(item).name === 'batch_read_media' && Array.isArray(batchIds) && batchIds.length && batchIds.every((id) => {
      const target = byId.get(string(id))
      return target && target.actor_id === item.actor_id && activityFor(target).read && activityFor(target).name === 'read_media'
    })) {
      hidden.add(item.id)
      continue
    }
    const targetId = string(fields.item_id) || string(receipt.item_id)
    const target = byId.get(targetId)
    if (!target || target.id === item.id || target.actor_id !== item.actor_id || !operationKinds.has(target.kind)
      || (object(target.activity ?? object(target.data).activity).type ?? object(target.data).type) === 'mcpToolCall' || activityFor(target).name !== activityFor(item).name) continue
    nativeByBusiness.set(targetId, [...nativeByBusiness.get(targetId) ?? [], item])
    hidden.add(item.id)
  }
  const rows: ActivityRow[] = []
  let pending: TranscriptItem[] = []
  const flush = () => {
    if (pending.length > 1) rows.push({ kind: 'reads', items: pending })
    else if (pending.length) rows.push({ kind: 'item', item: pending[0], native: nativeByBusiness.get(pending[0].id) })
    pending = []
  }
  for (const item of items) {
    if (hidden.has(item.id)) continue
    if (activityFor(item).read) pending.push(item)
    else { flush(); rows.push({ kind: 'item', item, native: nativeByBusiness.get(item.id) }) }
  }
  flush()
  return rows
}

/** IDs identify the recorded result; current selections never fill missing historical IDs. */
export function activityTarget(item: TranscriptItem, project: StudioProject): Selection {
  const { name, fields } = activityFor(item)
  if (string(fields.reference_plan_id)) return { kind: 'reference-plan', id: string(fields.reference_plan_id) }
  if (name === 'prepare_reference' || name === 'prepare_media') {
    const mediaId = string(fields.media_id)
    // The reference identifies the source board, never the prepared outcome.
    return mediaId ? { kind: 'media', id: mediaId } : { kind: 'change', id: item.id, item }
  }
  const referenceId = string(fields.reference_id) || string(fields.revision_id)
  const candidateId = string(fields.candidate_id)
  const stateId = string(fields.state_id) || (name === 'create_state' ? string(fields.id) : '')
  const clipId = string(fields.clip_id) || (candidateId ? project.candidates?.[candidateId]?.clip_id : '')
  const assetId = string(fields.asset_id) || (stateId ? project.states?.[stateId]?.asset_id : '')
  if (assetId && ['plan_voice', 'establish_voice', 'select_voice'].includes(name)) return { kind: 'asset', id: assetId, voiceReferenceId: string(fields.voice_reference_id) || undefined }
  if (referenceId) return { kind: 'reference', id: referenceId, referenceId, ...(stateId ? { stateId } : {}) }
  if (candidateId && clipId) return { kind: 'clip', id: clipId, candidateId }
  if (string(fields.media_id)) return { kind: 'media', id: string(fields.media_id) }
  if (assetId && ['create_asset', 'create_state', 'correct_state_scope'].includes(name)) return { kind: 'asset', id: assetId, ...(stateId ? { stateId } : {}) }
  // A Clip without a candidate ID cannot identify a generation outcome. The change
  // card is the exact historical record even when its result is unavailable.
  return { kind: 'change', id: item.id, item }
}

export function activityContext(item: TranscriptItem, project: StudioProject) {
  const { fields } = activityFor(item)
  const stateId = string(fields.state_id)
  const state = stateId ? project.states?.[stateId] : undefined
  const assetId = string(fields.asset_id) || state?.asset_id
  const asset = assetId ? project.assets?.[assetId] : undefined
  const clipId = string(fields.clip_id)
  const clip = clipId ? project.clips?.[clipId] : undefined
  const setId = string(fields.set_id) || clip?.set_id
  const set = setId ? project.sets?.[setId] : undefined
  return [asset?.name, state?.label, set?.title, clip?.title, fields.scope === 'global' ? tr("全局记忆") : ''].filter(Boolean).join(' / ')
}

export const isOperation = (item: TranscriptItem) => operationKinds.has(item.kind)
  || item.kind === 'status' && ['prepare_reference', 'prepare_media', 'establish_voice'].includes(activityFor(item).name)
