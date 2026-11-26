import { getLocale, tr } from "../../ui/locale"
import type { Candidate, StudioProject } from './model'

export const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
export const textValue = (value: unknown) => typeof value === 'string' ? value : ''
export const records = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(record) : []
export const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item) : []
export const unique = (values: string[]) => [...new Set(values)]
export function candidateSubmission(project: StudioProject, candidate: Candidate): Record<string, unknown> & { prompt: string } {
  const job = project.jobs?.[candidate.job_id]
  const provenance = record(record(job?.result).provenance)
  const request = record(job?.request)
  return { ...provenance, ...request, prompt: textValue(request.provider_prompt) || textValue(request.prompt) || textValue(provenance.submitted_prompt) || candidate.prompt }
}
export function seconds(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? tr("{{0}} 秒", Number(value.toFixed(2))) : '' }
export function recordedTime(value: unknown) {
  const date = typeof value === 'string' ? new Date(value) : null
  return date && Number.isFinite(date.getTime()) ? date.toLocaleString(getLocale(), { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : ''
}

export type ImageInput = {
  number: number; mediaId: string; assetId: string; stateId: string; revisionId: string
  assetName: string; stateLabel: string; view: string; purpose: string
  selection: string; reason: string; boundary: boolean; exact: boolean
}

/** Only the submitted job can establish crops and upload order. Never use a state's current selection. */
export function candidateImageInputs(project: StudioProject, candidate: Candidate): ImageInput[] {
  const job = project.jobs?.[candidate.job_id]
  const request = record(job?.request)
  const provenance = record(record(job?.result).provenance)
  const pinned = (key: string) => key in request ? request[key] : provenance[key]
  const submittedBindings = pinned('bindings')
  const bindings = Array.isArray(submittedBindings) ? records(submittedBindings) : candidate.bindings
  const rawInputs = pinned('reference_inputs')
  const hasPreparedInputs = rawInputs !== undefined && rawInputs !== null
  const inputs = records(rawInputs)
  const boundary = record(pinned('previous_end_state'))
  const boundaryId = textValue(boundary.frame_media_id)
  const result: ImageInput[] = boundaryId ? [{ number: 1, mediaId: boundaryId, assetId: '', stateId: '', revisionId: '', assetName: tr("上一片段实际末帧"), stateLabel: '', view: '', purpose: tr("接续上一片段的人物、动作、空间和机位。"), selection: '', reason: '', boundary: true, exact: true }] : []
  for (let index = 0; index < Math.max(bindings.length, inputs.length); index++) {
    const input = inputs[index] ?? {}
    const binding = hasPreparedInputs ? input : bindings[index] ?? {}
    const revisionId = textValue(binding.revision_id)
    const revision = project.references?.[revisionId]
    const assetId = textValue(binding.asset_id), stateId = textValue(binding.state_id)
    const preparation = record(input.preparation)
    const exact = hasPreparedInputs || Array.isArray(submittedBindings)
    result.push({
      number: result.length + 1,
      mediaId: hasPreparedInputs ? textValue(input.media_id) : exact ? revision?.media_id ?? '' : '',
      assetId, stateId, revisionId,
      assetName: textValue(input.asset_name) || project.assets?.[assetId]?.name || tr("参考资产"),
      stateLabel: textValue(input.state_label) || project.states?.[stateId]?.label || tr("剧情状态未记录"),
      view: textValue(input.view) || revision?.view || '',
      purpose: textValue(input.purpose) || textValue(preparation.purpose),
      selection: textValue(preparation.selection) || textValue(input.selection),
      reason: textValue(preparation.fallback_reason) || textValue(preparation.full_board_reason) || textValue(input.full_board_reason),
      boundary: false, exact,
    })
  }
  return result
}

export type EvidenceGroup = { id: string; title: string; mediaIds: string[] }
export function evidenceGroups(analysis: Record<string, unknown>, endFrameId?: string): EvidenceGroup[] {
  const used = new Set<string>()
  const groups: EvidenceGroup[] = []
  const add = (id: string, title: string, ids: string[]) => {
    const mediaIds = unique(ids).filter((id) => !used.has(id))
    if (!mediaIds.length) return
    mediaIds.forEach((id) => used.add(id))
    groups.push({ id, title, mediaIds })
  }
  const focused = records(analysis.evidence_pages).filter((page) => page.kind === 'focused_frames').map((page) => textValue(page.media_id)).filter(Boolean)
  add('sampled', tr("本段采样画面"), [...strings(analysis.sampled_frames_media_ids), textValue(analysis.sampled_frames_media_id)].filter((id) => !!id && !focused.includes(id)))
  add('focused', tr("局部补看画面"), focused)
  add('continuity', tr("前后段接续对照"), [textValue(analysis.seam_media_id), textValue(analysis.boundary_comparison_media_id)].filter(Boolean))
  add('previous-tail', tr("上一片段尾部"), strings(analysis.previous_tail_media_ids))
  add('repair-source', tr("修正前的画面"), strings(analysis.repair_source_media_ids))
  const finalFrame = endFrameId || textValue(analysis.end_frame_media_id)
  add('final-frame', tr("片段实际末帧"), finalFrame ? [finalFrame] : [])
  add('references', tr("本次参考图片"), strings(analysis.review_reference_media_ids))
  add('other', tr("其他检查画面"), strings(analysis.required_review_media_ids))
  return groups
}

export function originalReviews(candidate: Candidate): NonNullable<Candidate['review']>[] {
  const legacy = record(record(candidate.analysis?.user_selection).original_director_review)
  const values = [...(candidate.review_history ?? []), ...(legacy.decision && legacy.reason ? [legacy as NonNullable<Candidate['review']>] : []), ...(candidate.review ? [candidate.review] : [])]
  const seen = new Set<string>()
  return values.filter((review) => {
    const key = JSON.stringify(review)
    if (seen.has(key)) return false
    seen.add(key); return true
  }).sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))
}

export function selectionKind(candidate: Candidate) {
  if (candidate.selection?.kind) return candidate.selection.kind
  return record(candidate.analysis?.user_selection).temporary === true ? 'user_selection' : undefined
}

export function candidateVerdict(candidate: Candidate) {
  const selection = selectionKind(candidate)
  if (selection === 'system_fallback') return tr("系统兜底选用")
  if (selection === 'director_compromise') return tr("总导演妥协选用")
  if (selection === 'user_selection') return tr("用户临时选用")
  if (candidate.review?.decision === 'accept' && candidate.review.coverage_gaps?.length) return tr("有限审核后选用")
  if (candidate.review?.decision === 'accept') return tr("导演检查通过")
  if (candidate.review?.decision === 'reject') return tr("导演检查未通过")
  if (candidate.status === 'failed') return tr("生成失败")
  if (candidate.status === 'accepted') return tr("已选用，未记录验收结论")
  return textValue(candidate.analysis?.summary) || candidate.analysis?.review_mode === 'director' ? tr("等待导演验收") : tr("尚未完成检查")
}
