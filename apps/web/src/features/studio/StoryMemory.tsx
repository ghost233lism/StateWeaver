import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useState } from 'react'
import { useStudioData } from './data'
import { EntityLink, useEntityLinks } from './EntityLinks'
import { type Clip, type EntityPage, type MemoryEntry, type StudioProject } from './model'
import './StoryMemory.css'

export const globalStorySections = ['screenplay', 'events', 'directives'] as const
export const memorySectionLabels: Record<string, string> = {
  get screenplay() { return tr("剧本事实") }, get events() { return tr("事件与状态") }, get directives() { return tr("导演约定") }, get set_shared() { return tr("共用约定") },
  get clip_plan() { return tr("计划") }, get clip_actual() { return tr("已选实际结果") }, get clip_continuation() { return tr("接续要求") }, get legacy() { return tr("原始记录") }, get log() { return tr("任务记录") },
  get history() { return tr("历史原文") },
}
export function storySection(entry: MemoryEntry) {
  if (entry.status === 'history' || entry.status === 'superseded') return 'history'
  if (entry.section && !['legacy', 'log'].includes(entry.section)) return entry.section
  return entry.kind === 'observation' ? 'events' : 'directives'
}
export function storySections(entries: MemoryEntry[]) {
  return [...globalStorySections, ...(entries.some((e) => storySection(e) === 'history') ? ['history'] : [])]
}

export function relatesToClip(entry: MemoryEntry, clip: Clip | undefined) {
  if (!clip) return false
  const scope = entry.scope
  const dimensions = [
    [scope?.clip_ids ?? [], [clip.id ?? '']],
    [scope?.set_ids ?? [], [clip.set_id]],
    [scope?.event_ids ?? [], clip.story_event_ids ?? []],
    [scope?.asset_ids ?? [], clip.asset_plan?.map((p) => p.asset_id) ?? []],
  ]
  return dimensions.some(([ids]) => ids.length > 0) && dimensions.every(([ids, current]) => !ids.length || ids.some((id) => current.includes(id)))
}

export function useMemoryEntries(project: StudioProject, scope: string) {
  const source = useStudioData()
  const [mode, setMode] = useState<'effective' | 'all'>('effective')
  const [page, setPage] = useState<EntityPage | null>(null)
  const [after, setAfter] = useState<string>()
  const [back, setBack] = useState<(string | undefined)[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const invalidation = source?.invalidations?.[`memory:${scope}`]
  useEffect(() => { setAfter(undefined); setBack([]); setPage(null) }, [scope, mode, project.id, invalidation])
  useEffect(() => {
    if (!source) return
    const controller = new AbortController()
    setLoading(true); setError('')
    const request = source.client.memory
      ? source.client.memory(source.projectId, scope, mode, after, controller.signal)
      : source.client.history(source.projectId, 'memory_entries', scope, after, controller.signal)
    void request.then((result) => { if (!controller.signal.aborted) setPage(result) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "记忆加载失败") })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [source?.client, source?.projectId, scope, mode, after, retry, invalidation])
  const saved = scope === 'global' ? project.global_memory?.story : project.sets?.[scope]?.memory?.story
  const records = (page?.items as MemoryEntry[] | undefined) ?? saved ?? []
  const entries = records.filter((entry) => mode === 'all' || (!entry.status || entry.status === 'effective') && entry.section !== 'log')
  return {
    entries, mode,
    switcher: <div className="memory-version-switch" aria-label={tr("记忆版本")}><button aria-pressed={mode === 'effective'} onClick={() => setMode('effective')}>{tr("当前有效")}</button><button aria-pressed={mode === 'all'} onClick={() => setMode('all')}>{tr("包含历史")}</button></div>,
    controls: <div className="studio-pagination">{loading && <span role="status">{tr("正在加载记忆…")}</span>}{error && <span role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((x) => x + 1)}>{tr("重试")}</button></span>}{back.length > 0 && <button className="studio-text-button" disabled={loading} onClick={() => { setAfter(back.at(-1)); setBack(back.slice(0, -1)) }}>{tr("上一页记录")}</button>}{page?.has_more && <button className="studio-text-button" disabled={loading} onClick={() => { setBack([...back, after]); setAfter(page.next_cursor ?? undefined) }}>{tr("下一页记录")}</button>}</div>,
  }
}

export const basisLabels = { get fact() { return tr("已确认") }, get plan() { return tr("计划") }, get observed() { return tr("实际观察") }, get unknown() { return tr("待核对") }, get provisional() { return tr("临时制作决定") } }

export function MemoryEntryBadges({ entry }: { entry: MemoryEntry }) {
  useLocale()

  const showBasis = entry.basis && !(entry.status === 'history' && ['legacy', 'log'].includes(entry.section ?? 'legacy'))
  return <div className="memory-entry-tags">
    {showBasis && <span className={`memory-basis memory-basis--${entry.basis}`}>{basisLabels[entry.basis]}</span>}
    {entry.status === 'superseded' && <span>{tr("已被替代")}</span>}{entry.status === 'history' && <span>{tr("历史原文")}</span>}
    {(entry.version ?? 1) > 1 && <span>{tr("版本")} {entry.version}</span>}
  </div>
}

export function MemoryEntryMetadata({ entry }: { entry: MemoryEntry }) {
  useLocale()

  const context = useEntityLinks()
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const sources = entry.sources ?? []
  const hasSources = sources.length || entry.source_candidate_id || entry.reason || entry.normalization_note || entry.superseded_by || entry.supersedes_ids?.length
  const scope = entry.scope
  const events = scope?.event_ids ?? []
  return <>
    <MemoryEntryBadges entry={entry} />
    <div className="memory-entry-tags">
      {events.map((id) => <EntityLink key={id} id={id}>{context?.project.story_events?.[id]?.title ?? tr("关联剧情事件")}</EntityLink>)}
      {scope?.set_ids?.map((id) => <EntityLink key={id} id={id}>{context?.project.sets?.[id]?.title ?? tr("关联片场")}</EntityLink>)}
      {scope?.clip_ids?.map((id) => <EntityLink key={id} id={id}>{context?.project.clips?.[id]?.title ?? tr("关联片段")}</EntityLink>)}
      {scope?.asset_ids?.map((id) => <EntityLink key={id} id={id}>{context?.project.assets?.[id]?.name ?? tr("关联资产")}</EntityLink>)}
    </div>
    {(scope?.starts_at || scope?.ends_at) && <p className="studio-note">{scope.starts_at && <>{tr("开始于")} <EntityLink id={scope.starts_at.event_id} />{scope.starts_at.position === 'before' ? tr("之前") : tr("之后")}</>}{scope.ends_at && <>{tr("；结束于")} <EntityLink id={scope.ends_at.event_id} />{scope.ends_at.position === 'before' ? tr("之前") : tr("之后")}</>}</p>}
    {hasSources ? <details className="memory-entry-sources" open={sourcesOpen} onToggle={(e) => setSourcesOpen(e.currentTarget.open)}><summary onClick={(e) => { e.preventDefault(); setSourcesOpen((v) => !v) }}>{tr("来源与修改依据")}</summary>{sourcesOpen && <div>
      {entry.reason && <p>{entry.reason}</p>}{entry.normalization_note && <p className="studio-note">{entry.normalization_note}</p>}
      {sources.map((source, index) => <p key={`${source.id}:${index}`}>{source.kind === 'screenplay_fact' ? tr("来源：用户已确认剧本") : <EntityLink id={source.id} />}{source.note && <span>：{source.note}</span>}</p>)}
      {entry.source_candidate_id && !sources.some((s) => s.id === entry.source_candidate_id) && <p>{tr("实际来源：")}<EntityLink id={entry.source_candidate_id} /></p>}
      {entry.supersedes_ids?.map((id) => <p key={id}>{tr("替代的旧版：")}<EntityLink id={id} /></p>)}
      {entry.superseded_by && <p>{tr("当前版本：")}<EntityLink id={entry.superseded_by} /></p>}
      {entry.approved_state_ids?.length ? <p>{tr("截图仅适用于：")}{entry.approved_state_ids.map((id) => <EntityLink key={id} id={id}>{context?.project.states?.[id]?.label ?? tr("已认可状态")}</EntityLink>)}{tr("；正确局部：")}{entry.approved_attributes?.join('、')}{tr("。用途：")}{entry.approval_purpose}{tr("。截取于")} {entry.source_seconds} {tr("秒。")}</p> : null}
      {entry.actor_id && <p>{tr("记录者：")}<EntityLink id={entry.actor_id} /></p>}
    </div>}</details> : null}
  </>
}
