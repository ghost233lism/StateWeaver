import { tr } from "../../ui/locale"
import { roleLabels, viewLabel, type Selection, type StudioProject } from './model'

const own = <T,>(collection: Record<string, T> | undefined, id: string): T | undefined => collection && Object.hasOwn(collection, id) ? collection[id] : undefined
const opaqueId = /^(?:[a-f\d]{32}|[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12})$/i
const text = (value: unknown) => typeof value === 'string' ? value : ''
const ordinal = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : undefined
const readable = (value: string | undefined, fallback: string) => value?.trim() && !opaqueId.test(value.trim()) ? value : fallback

function candidateOwner(project: StudioProject, id: string) {
  return Object.entries(project.clips ?? {}).find(([, clip]) => clip.accepted_candidate_id === id)
}

function memoryEntry(project: StudioProject, id: string) {
  const global = project.global_memory?.story?.find((entry) => entry.id === id)
  if (global) return { scope: 'global', name: tr("全局记忆"), entry: global }
  for (const [setId, set] of Object.entries(project.sets ?? {})) {
    const entry = set.memory?.story?.find((entry) => entry.id === id)
    if (entry) return { scope: setId, name: tr("{{0}} · 片场记忆", readable(set.title, tr("片场"))), entry }
  }
  return undefined
}

/** Resolve only identities already in memory; an unknown opaque ID is resolved on click. */
export function entityTarget(project: StudioProject, id: string): Selection | null {
  if (id && id === project.id) return { kind: 'memory', id: 'global' }
  if (own(project.actors, id)) return { kind: 'actor', id }
  const set = own(project.sets, id)
  if (set) return { kind: 'actor', id: set.actor_id }
  if (own(project.clips, id)) return { kind: 'clip', id }
  if (own(project.reference_plans, id)) return { kind: 'reference-plan', id }
  if (own(project.visual_tasks, id)) return { kind: 'visual-task', id }
  if (own(project.story_events, id)) return { kind: 'story-event', id }
  const candidate = own(project.candidates, id)
  if (candidate) return { kind: 'clip', id: candidate.clip_id, candidateId: id }
  if (own(project.assets, id)) return { kind: 'asset', id }
  const state = own(project.states, id)
  if (state) return { kind: 'asset', id: state.asset_id, stateId: id }
  const reference = own(project.references, id)
  if (reference) return { kind: 'reference', id, referenceId: id, stateId: reference.state_id }
  if (own(project.media, id)) return { kind: 'media', id }
  const record = own(project.transcript, id)
  if (record) return { kind: 'actor', id: record.actor_id }
  const proposal = own(project.proposals, id)
  if (proposal) return { kind: 'actor', id: own(project.sets, proposal.set_id)?.actor_id || proposal.actor_id }
  const memory = memoryEntry(project, id)
  if (memory) return { kind: 'memory', id: memory.scope, entryId: id }
  const job = own(project.jobs, id)
  if (job) {
    if (job.kind === 'reference') {
      const mediaId = text(job.result?.media_id)
      return job.status === 'complete' && mediaId ? { kind: 'media', id: mediaId } : { kind: 'actor', id: job.actor_id }
    }
    if (job.kind === 'image') {
      const stateId = own(project.references, job.target_id)?.state_id
      return { kind: 'reference', id: job.target_id, referenceId: job.target_id, ...(stateId ? { stateId } : {}) }
    }
    const result = own(project.candidates, job.target_id)
    if ((job.kind === 'video' || job.kind === 'analysis') && result) return { kind: 'clip', id: result.clip_id, candidateId: job.target_id }
    // A later export must never replace an assembly job's historical result.
    return { kind: 'actor', id: job.actor_id }
  }
  const owner = candidateOwner(project, id)
  if (owner) return { kind: 'clip', id: owner[0], candidateId: id }
  return opaqueId.test(id) ? { kind: 'entity', id } : null
}

function stateName(project: StudioProject, stateId: string) {
  const state = own(project.states, stateId)
  return state ? [readable(own(project.assets, state.asset_id)?.name, tr("视觉资产")), readable(state.label, tr("剧情状态"))].join(' · ') : tr("资产状态")
}

/** Labels describe an exact record, without deriving revision ordinals from partial arrays. */
export function entityLabel(project: StudioProject, id: string): string {
  if (id && id === project.id) return readable(project.title, tr("项目全局记忆"))
  const actor = own(project.actors, id)
  if (actor) return actor.role === 'set_director' && actor.set_id ? `${readable(own(project.sets, actor.set_id)?.title, tr("片场"))} · ${roleLabels[actor.role]}` : roleLabels[actor.role]
  const set = own(project.sets, id)
  if (set) return readable(set.title, tr("片场"))
  const clip = own(project.clips, id)
  if (clip) return readable(clip.title, tr("视频片段"))
  const plan = own(project.reference_plans, id)
  if (plan) return tr("{{0}} · 参考清单第 {{1}} 版", readable(own(project.clips, plan.clip_id)?.title, tr("片段")), plan.revision)
  const event = own(project.story_events, id)
  if (event) return event.title
  const visualTask = own(project.visual_tasks, id)
  if (visualTask) return tr("看图任务：{{0}}", visualTask.question)
  const candidate = own(project.candidates, id)
  if (candidate) {
    const number = ordinal((candidate as unknown as Record<string, unknown>).history_ordinal)
    return `${readable(own(project.clips, candidate.clip_id)?.title, tr("视频片段"))} · ${number ? tr("第 {{0}} 次生成", number) : tr("某次生成")}`
  }
  const asset = own(project.assets, id)
  if (asset) return readable(asset.name, tr("视觉资产"))
  if (own(project.states, id)) return stateName(project, id)
  const reference = own(project.references, id)
  if (reference) return `${stateName(project, reference.state_id)} · ${readable(viewLabel(reference.view), tr("参考图"))}`
  const media = own(project.media, id)
  if (media) return media.mime_type.startsWith('video/') ? tr("视频素材") : media.mime_type.startsWith('image/') ? tr("图像素材") : media.mime_type.startsWith('audio/') ? tr("音频素材") : tr("素材记录")
  const record = own(project.transcript, id)
  if (record) return record.kind === 'memory_change' ? tr("记忆更新所在对话") : record.kind === 'asset_change' ? tr("资产更新所在对话") : tr("导演输出所在对话")
  const proposal = own(project.proposals, id)
  if (proposal) return tr("{{0}} · 全局修改建议", readable(own(project.sets, proposal.set_id)?.title, tr("片场")))
  const memory = memoryEntry(project, id)
  if (memory) return `${memory.name} · ${{ constraint: tr("已确认事实"), plan: tr("制作计划"), observation: tr("实际观察") }[memory.entry.kind]}`
  const job = own(project.jobs, id)
  if (job) return { image: tr("参考图生成任务"), reference: tr("参考素材准备任务"), video: tr("视频生成任务"), analysis: tr("片段检查任务"), assembly: tr("合成任务") }[job.kind]
  const owner = candidateOwner(project, id)
  return owner ? tr("{{0}} · 已选用结果", readable(owner[1].title, tr("视频片段"))) : tr("关联记录")
}

/** Simple names already present on the canvas need no label API; history needs exact ordinals. */
export function needsEntityLabel(project: StudioProject, id: string): boolean {
  if (id && id === project.id) return false
  const actor = own(project.actors, id)
  if (actor) return actor.role === 'set_director' && !own(project.sets, actor.set_id ?? '')?.title
  const name = own(project.sets, id)?.title || own(project.clips, id)?.title || own(project.assets, id)?.name
  if (name?.trim() && !opaqueId.test(name.trim())) return false
  const state = own(project.states, id)
  if (state && readable(state.label, '') && readable(own(project.assets, state.asset_id)?.name, '')) return false
  return true
}

type Range = { start: number; end: number }
export type EntityTextPart = { text: string; id?: string; start: number }

/** URLs, paths, email addresses and math stay literal even in non-Markdown text. */
function protectedRanges(value: string): Range[] {
  const patterns = [
    /(?:[a-z][a-z\d+.-]*:\/\/|(?:mailto|tel|data|urn):|www\.)[^\s<>"`，。；：！？、（）【】《》]+/gi,
    /(?:\.{0,2}\/|[a-z]:\\)[a-z\d_./?=&%#:+~@!$;()\\-]+/gi,
    /[a-z\d._%+-]+@[a-z\d.-]+\.[a-z]+/gi,
    /(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|(?<!\\)\$(?!\$)[^\n$]*?(?<!\\)\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]/g,
  ]
  return patterns.flatMap((pattern) => [...value.matchAll(pattern)].map((match) => ({ start: match.index!, end: match.index! + match[0].length })))
}
const overlaps = (ranges: Range[], start: number, end: number) => ranges.some((range) => start < range.end && end > range.start)
const identifierBoundary = (value: string, start: number, end: number) => !/[a-z\d_.-]/i.test(value[start - 1] ?? '')
  && !/[a-z\d_-]/i.test(value[end] ?? '') && !(value[end] === '.' && /[a-z\d_-]/i.test(value[end + 1] ?? ''))

/** A display excerpt must not turn a complete identity into an unresolvable fragment. */
export function entityExcerpt(value: string, maxCharacters: number): string {
  if (!Number.isFinite(maxCharacters) || maxCharacters <= 0) return ''
  const preview = Array.from(value).slice(0, Math.floor(maxCharacters)).join('')
  if (preview.length === value.length) return preview
  for (const match of value.matchAll(/[a-z\d_][a-z\d_.-]*/gi)) {
    const identifier = match[0].replace(/\.+$/, '')
    const start = match.index!
    const end = start + identifier.length
    if (start >= preview.length) break
    if (end <= preview.length) continue
    return start === 0 && opaqueId.test(identifier) ? identifier : value.slice(0, start)
  }
  return preview
}

export function entityTextParts(value: string, project: StudioProject, permit?: (id: string, start: number) => boolean): EntityTextPart[] {
  const protectedText = protectedRanges(value)
  const parts: EntityTextPart[] = []
  const identities = new Map<string, boolean>()
  let offset = 0
  for (const match of value.matchAll(/[a-z\d_][a-z\d_.-]*/gi)) {
    // A sentence-final period is punctuation; an ID embedded in a filename is not.
    const id = match[0].replace(/\.+$/, '')
    const start = match.index!
    const end = start + id.length
    if (!identities.has(id)) identities.set(id, Boolean(entityTarget(project, id)))
    if (!identities.get(id) || !identifierBoundary(value, start, end)) continue
    const permitted = permit?.(id, start) ?? true
    if (!permitted || overlaps(protectedText, start, end)) continue
    if (start > offset) parts.push({ text: value.slice(offset, start), start: offset })
    parts.push({ text: entityLabel(project, id), id, start })
    offset = end
  }
  if (offset < value.length || !parts.length) parts.push({ text: value.slice(offset), start: offset })
  return parts
}

type MarkdownNode = { type: string; value?: string; children?: MarkdownNode[]; position?: { start: { offset?: number }; end: { offset?: number } }; [key: string]: unknown }
const excludedNodes = new Set(['link', 'linkReference', 'definition', 'image', 'imageReference', 'code', 'html', 'math', 'inlineMath'])

/** Transform AST text, never Markdown source, URLs, code blocks or image alternatives. */
export function remarkEntityLinks(options: { project: StudioProject; source: string }) {
  return (tree: unknown) => {
    const excluded = protectedRanges(options.source)
    const convert = (node: MarkdownNode) => {
      if (!node.children || excludedNodes.has(node.type)) return
      const children: MarkdownNode[] = []
      for (const child of node.children) {
        if (child.type !== 'text' && child.type !== 'inlineCode') { convert(child); children.push(child); continue }
        const value = child.value ?? ''
        const start = child.position?.start.offset
        const end = child.position?.end.offset
        let sourceOffset = start ?? 0
        const permit = (id: string) => {
          if (start === undefined || end === undefined) return true
          let found = options.source.indexOf(id, sourceOffset)
          while (found >= 0 && found + id.length <= end && !identifierBoundary(options.source, found, found + id.length)) found = options.source.indexOf(id, found + id.length)
          if (found < 0 || found + id.length > end) return false
          sourceOffset = found + id.length
          return !overlaps(excluded, found, sourceOffset)
        }
        const parts = entityTextParts(value, options.project, permit)
        const pureInlineId = parts.length === 1 && parts[0].id && value === parts[0].id
        if (child.type === 'inlineCode' && !pureInlineId) { children.push(child); continue }
        for (const part of parts) children.push(part.id ? {
          type: 'link', url: `#studio-entity/${encodeURIComponent(part.id)}`,
          data: { hProperties: { 'data-studio-entity': part.id } }, children: [{ type: 'text', value: part.text }],
        } : { type: 'text', value: part.text })
      }
      node.children = children
    }
    convert(tree as MarkdownNode)
  }
}
