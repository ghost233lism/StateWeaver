import { getLocale, tr } from "../../ui/locale"
import type { components } from '../../generated/studio-schema'

type Schemas = components['schemas']
export type StudioProject = Schemas['StudioProject']
export type DirectorMode = NonNullable<StudioProject['director_mode']>
export type Actor = Schemas['Actor']
export type ProductionSet = Schemas['ProductionSet']
export type ProductionStatus = Schemas['ProductionStatus']
export type ProductionETA = Schemas['ProductionETA']
export type Clip = Schemas['Clip']
export type ClipPreparationUpdate = Schemas['ClipPreparationUpdate']
export type Candidate = Schemas['Candidate']
export type ReviewGap = Schemas['ReviewGap']
export type Memory = Schemas['Memory']
export type MemoryEntry = Schemas['MemoryEntry']
export type ReferencePlan = Schemas['ReferencePlan']
export type Asset = Schemas['LogicalAsset']
export type AssetState = Schemas['AssetState']
export type Reference = Schemas['ReferenceRevision']
export type Media = Schemas['Media']
export type SetPreview = Schemas['SetPreview']
export type SetDependencyReport = Schemas['SetDependencyReport']
export type TranscriptItem = Schemas['TranscriptItem']
export type StudioEvent = Schemas['StudioEvent']
export type EventBatch = Schemas['EventBatch']
export type CreateProject = Omit<Schemas['CreateProject'], 'config'> & {
  config?: Partial<NonNullable<Schemas['CreateProject']['config']>>
}
export type UserAction = Schemas['UserAction']
export type ProjectList = Schemas['ProjectList']
export type ProjectSummary = Schemas['ProjectSummary']
export type StudioSnapshot = Schemas['StudioSnapshot']
export type CommandResult = Schemas['CommandResult']
export type TranscriptPage = Schemas['TranscriptPage']
export type EntityPage = Schemas['EntityPage']
export type EntityLabelBatch = Schemas['EntityLabelBatch']
export type BodyChunk = Schemas['BodyChunk']
export type ClipReadiness = Schemas['ClipReadiness']
export type QualityProgress = Schemas['QualityProgress']
export type VisualAnalysisTask = Schemas['VisualAnalysisTask']
export type CandidateComparison = Schemas['CandidateComparison']
export type WorkspaceLayout = Schemas['WorkspaceLayout']
export type WorkspaceLayoutUpdate = Schemas['WorkspaceLayoutUpdate']
export type Selection = {
  kind: 'actor' | 'memory' | 'clip' | 'asset' | 'reference' | 'media' | 'change' | 'entity' | 'reference-plan' | 'story-event' | 'set-preview' | 'visual-task'
  id: string
  candidateId?: string
  clipId?: string
  stateId?: string
  referenceId?: string
  voiceReferenceId?: string
  entryId?: string
  item?: TranscriptItem
}

export function entries<T extends { id?: string }>(record: Record<string, T> | undefined): (T & { id: string })[] {
  return Object.entries(record ?? {}).map(([id, value]) => ({ ...value, id: value.id ?? id }))
}

export function orderedSets(project: StudioProject) {
  return entries(project.sets).sort((a, b) => a.order - b.order)
}

export function generationDependencies(set: ProductionSet): string[] {
  const observedDependencies = (set.asset_handoffs ?? [])
    .flatMap((item) => item.mode === 'observed' && item.source_set_id ? [item.source_set_id] : [])
  return [...new Set([...(set.depends_on ?? []), ...observedDependencies])]
}

export function setClips(project: StudioProject, setId: string) {
  const activeIds = new Set(project.sets?.[setId]?.clip_ids ?? [])
  return entries(project.clips).filter((clip) => clip.set_id === setId && activeIds.has(clip.id) && !clip.archived_at)
    .sort((a, b) => a.order - b.order)
}

export function activeClips(project: StudioProject) { return orderedSets(project).flatMap((set) => setClips(project, set.id)) }

export const roleLabels: Record<Actor['role'], string> = {
  get screenwriter() { return tr("编剧") }, get executive() { return tr("总导演") }, get set_director() { return tr("片场导演") }, get visual_analyst() { return tr("看图分析") },
  get task_analyst() { return tr("专项分析") }, get material_executor() { return tr("素材执行") },
}

/** Keep original names for links/history; omit only a matching padded prefix. */
export function unitDisplayTitle(set: Pick<ProductionSet, 'title' | 'order'>) {
  const prefix = set.title.match(/^(\d{2,3})[\s·.、:：\-–—]+(.+)$/u)
  return prefix && prefix[1] === String(set.order + 1).padStart(2, '0') ? prefix[2] : set.title
}

export const statusLabels: Record<string, string> = {
  get idle() { return tr("待命") }, get ready() { return tr("准备开始") }, get running() { return tr("正在工作") }, get waiting() { return tr("等待结果") }, get paused() { return tr("已暂停") },
  get blocked() { return tr("需要处理") }, get complete() { return tr("已完成") }, get screenwriting() { return tr("编剧中") }, get preparing() { return tr("准备制作") },
  get producing() { return tr("制作中") }, get planned() { return tr("待制作") }, get generating() { return tr("生成中") }, get review() { return tr("检查中") },
  get accepted() { return tr("已选用") }, get rejected() { return tr("未选用") }, get failed() { return tr("失败") }, get pending() { return tr("等待生成") },
  get candidate() { return tr("候选") }, get selected() { return tr("已选用") }, get queued() { return tr("等待提交") }, get submitting() { return tr("正在提交") }, get ambiguous() { return tr("待核实") },
  get repair() { return tr("定向修正中") }, get escalated() { return tr("等待总导演接管") },
}

export function label(status: string) { return statusLabels[status] ?? status }

export function effectiveProjectStatus(project: StudioProject) {
  if (project.status === 'complete' || project.status === 'screenwriting') return project.status
  return entries(project.actors).some((actor) => actor.status === 'blocked' && !actor.paused && actor.last_error)
    ? 'blocked' : project.status
}

export const viewLabels: Record<string, string> = {
  get close_up() { return tr("面部特写") }, get face_closeup() { return tr("面部特写") }, get face_close_up() { return tr("面部特写") }, get closeup() { return tr("面部特写") },
  get full_body() { return tr("全身图") }, get three_view() { return tr("三视图") }, get three_views() { return tr("三视图") },
  get expression_grid() { return tr("表情九宫格") }, get expression_sheet() { return tr("表情九宫格") }, get expressions() { return tr("表情九宫格") },
  get establishing() { return tr("场景全貌") }, get wide() { return tr("场景全貌") }, get detail() { return tr("细节") }, get master() { return tr("主参考图") },
}

export function viewLabel(view: string) { return viewLabels[view] ?? view }

/** A partial history cannot establish an ordinal; use recorded time instead of an ID. */
export function referenceName(reference: Reference, ordinal?: number) {
  if (ordinal !== undefined && Number.isSafeInteger(ordinal) && ordinal > 0) return tr("修订 {{0}}", ordinal)
  const time = reference.created_at ? new Date(reference.created_at) : null
  return time && Number.isFinite(time.getTime()) ? tr("修订 · {{0}}", time.toLocaleString(getLocale(), { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })) : tr("参考图修订")
}

export function newestCandidate(project: StudioProject, clip: Clip) {
  const candidates = entries(project.candidates).filter((candidate) => candidate.clip_id === clip.id)
  return candidates.find((candidate) => candidate.id === clip.accepted_candidate_id)
    ?? candidates.sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? '')).at(-1)
}

export function transcriptFor(project: StudioProject, actorId: string): TranscriptItem[] {
  return Object.values(project.transcript ?? {}).filter((item) => item.actor_id === actorId)
    .sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))
}

/** A corrupt/cyclic parent chain is displayed once, never followed indefinitely. */
export function revisionTree(references: (Reference & { id: string })[]) {
  const ids = new Set(references.map((reference) => reference.id))
  const children = new Map<string, (Reference & { id: string })[]>()
  for (const reference of references) {
    if (reference.parent_id) { const siblings = children.get(reference.parent_id) ?? []; siblings.push(reference); children.set(reference.parent_id, siblings) }
  }
  const visited = new Set<string>()
  const result: { reference: Reference & { id: string }; depth: number }[] = []
  const visit = (reference: Reference & { id: string }, depth: number) => {
    const pending = [{ reference, depth }]
    while (pending.length) {
      const node = pending.pop()!
      if (visited.has(node.reference.id)) continue
      visited.add(node.reference.id); result.push(node)
      const descendants = children.get(node.reference.id) ?? []
      for (let index = descendants.length - 1; index >= 0; index--) pending.push({ reference: descendants[index], depth: node.depth + 1 })
    }
  }
  references.filter((reference) => !reference.parent_id || !ids.has(reference.parent_id)).forEach((reference) => visit(reference, 0))
  references.forEach((reference) => visit(reference, 0))
  return result
}
