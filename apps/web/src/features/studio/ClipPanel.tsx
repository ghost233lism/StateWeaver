import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ArrowLeft, ChevronUp } from 'lucide-react'
import { entries, label, type StudioProject, type Selection } from './model'
import { Status } from './Status'
import { MediaPreview } from './Media'
import { EntityLinkProvider, EntityText } from './EntityLinks'
import { LazyDetails, useEntityDetail, useEntityHistory, useStudioData } from './data'
import { CandidateReviewDetails, CandidateReviewSummary } from './CandidateReview'
import { CandidateReferences } from './CandidateReferences'
import { CandidateContinuity, QualityEpisodeDetail } from './ContinuityDetails'
import { CandidateComparisonPanel, ClipReadinessPanel } from './ProductionTools'
import { ReferencePlanPanel } from './ReferencePlanPanel'
import { ClipStatePlan } from './SetStoryMemory'
import { candidateVerdict } from './candidate-details'
import { Markdown } from './Markdown'
import { ReadingText } from './ReadingText'
import { ClipPlayerFrame } from './ClipPlayerFrame'
import { ClipPreparationPanel } from './ClipPreparation'
import './ClipPanel.css'

const tabs = [{ id: 'overview', get label() { return tr("概览") } }, { id: 'references', get label() { return tr("参考素材") } }, { id: 'checks', get label() { return tr("检查记录") } }, { id: 'history', get label() { return tr("生成历史") } }] as const
type ClipTab = typeof tabs[number]['id']

/** The comparison receipt knows the latest playable candidate beyond the first history page. */
function useLatestPlayable(project: StudioProject, clipId: string, enabled: boolean) {
  const source = useStudioData()
  const [value, setValue] = useState<{ id?: string; error?: string }>()
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    if (!enabled || !source?.client.comparison) return
    const controller = new AbortController()
    setValue(undefined)
    void source.client.comparison(source.projectId, clipId, undefined, controller.signal).then((result) => {
      if (!controller.signal.aborted) {
        if (result.clip_id !== clipId) throw new Error("返回的候选不属于当前片段，请重试。")
        setValue({ id: result.default_pair?.[0] })
      }
    }).catch((error: unknown) => { if (!controller.signal.aborted) setValue({ error: error instanceof Error ? error.message : "最新视频定位失败" }) })
    return () => controller.abort()
  }, [enabled, source?.client, project.id, clipId, source?.invalidations?.[`clip:${clipId}`], retry])
  return { value, remote: enabled && Boolean(source?.client.comparison), retry: () => setRetry((n) => n + 1) }
}

export function ClipPanel({ project: initialProject, clipId, initialCandidateId, onSelect }: {
  project: StudioProject; clipId: string; initialCandidateId?: string; onSelect: (selection: Selection) => void
}) {
  useLocale()

  const source = useStudioData()
  const history = useEntityHistory(initialProject, 'candidates', clipId)
  const [candidateId, setCandidateId] = useState(initialCandidateId)
  const accepted = history.project.clips?.[clipId]?.accepted_candidate_id
  const recent = useLatestPlayable(initialProject, clipId, !candidateId && !accepted)
  const known = entries(history.project.candidates).filter((item) => item.clip_id === clipId).sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))
  const fallback = known.filter((item) => item.media_id).at(-1)?.id
  const defaultCandidateId = recent.remote
    ? recent.value?.id ?? (recent.value && !recent.value.error ? known.at(-1)?.id : undefined)
    : fallback ?? known.at(-1)?.id
  const effectiveCandidateId = candidateId ?? accepted ?? defaultCandidateId
  const candidateDetail = useEntityDetail(history.project, effectiveCandidateId ? `candidate:${effectiveCandidateId}` : undefined)
  const project = candidateDetail.project
  const clip = project.clips![clipId]
  const candidates = entries(project.candidates).filter((item) => item.clip_id === clipId && (!history.ids || history.ids.includes(item.id) || item.id === effectiveCandidateId || item.id === clip.accepted_candidate_id))
    .sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))
  const candidate = effectiveCandidateId ? project.candidates?.[effectiveCandidateId] : undefined
  const validCandidate = candidate?.clip_id === clipId ? candidate : undefined
  const attemptLabels = Object.fromEntries(candidates.map((item, index) => {
    const ordinal = history.items?.find((entry) => entry.id === item.id)?.history_ordinal
    return [item.id, typeof ordinal === 'number' ? tr("第 {{0}} 次生成", ordinal) : source ? tr("指定生成结果") : tr("第 {{0}} 次生成", index + 1)]
  }))
  const set = project.sets?.[clip.set_id]
  const [tab, setTab] = useState<ClipTab>('overview')
  const [visited, setVisited] = useState<Set<ClipTab>>(() => new Set(['overview']))
  const panel = useRef<HTMLDivElement>(null)
  const player = useRef<HTMLDivElement>(null)
  const positions = useRef<Partial<Record<ClipTab, number>>>({})
  const id = useId()
  const chooseTab = (next: ClipTab) => {
    if (next === tab) return
    if (panel.current) positions.current[tab] = panel.current.scrollTop
    panel.current?.querySelectorAll<HTMLMediaElement>('video, audio').forEach((media) => { if (!media.paused) media.pause() })
    setVisited((current) => current.has(next) ? current : new Set([...current, next]))
    setTab(next)
  }
  useLayoutEffect(() => { if (panel.current) panel.current.scrollTop = positions.current[tab] ?? 0 }, [tab])
  const chooseCandidate = (next: string) => {
    if (next === effectiveCandidateId) return
    setCandidateId(next)
    positions.current = {}
    if (panel.current) panel.current.scrollTop = 0
  }
  const latest = known.at(-1)
  const latestJob = entries(project.jobs).filter((job) => job.kind === 'video' && known.some((item) => item.job_id === job.id)).sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? '')).at(-1)
  const pending = latest && ['failed', 'generating', 'review'].includes(latest.status) ? latest : undefined

  return <EntityLinkProvider project={project} onSelect={onSelect}><div className="studio-clip-panel studio-clip-workspace">
    <ClipPlayerFrame onCollapse={() => player.current?.querySelector('video')?.pause()}>{({ collapse }) => <div className="studio-clip-player">
      <div className="studio-clip-meta"><Status value={clip.status} /><span>{clip.duration} {tr("秒")}</span>{set && <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: set.actor_id })}><ArrowLeft size={14} />{tr("片场导演")}</button>}</div>
      <div className="studio-clip-viewing"><span>{effectiveCandidateId ? `${attemptLabels[effectiveCandidateId] ?? tr("指定生成结果")}${effectiveCandidateId === clip.accepted_candidate_id ? tr(" · 成片选用") : tr(" · 当前查看")}` : tr("视频预览")}</span><button className="studio-text-button" aria-expanded="true" aria-controls={`${id}-player`} onClick={collapse}><ChevronUp size={14} />{tr("收起播放器")}</button></div>
      <div ref={player} id={`${id}-player`} className="studio-clip-screen" onPlayCapture={() => panel.current?.querySelectorAll<HTMLMediaElement>('video, audio').forEach((media) => { if (!media.paused) media.pause() })}>
        {validCandidate ? <MediaPreview key={effectiveCandidateId} media={validCandidate.media_id ? project.media?.[validCandidate.media_id] : undefined} title={clip.title} /> : <div className="studio-clip-video-empty">{effectiveCandidateId ? tr("正在定位指定生成结果；不会替换为其他版本。") : recent.value?.error ? tr("视频定位失败，可重试或打开生成历史。") : recent.remote && !recent.value ? tr("正在定位最新可播放视频…") : tr("片段尚无可播放视频。完成后可在这里查看。")}</div>}
      </div>
      {validCandidate && <p className="studio-clip-verdict">{candidateVerdict(validCandidate)}</p>}
      {candidateDetail.status}
      {recent.value?.error && <p role="alert">{trSystem(recent.value.error)}<button className="studio-text-button" onClick={recent.retry}>{tr("重试")}</button></p>}
      {pending && (pending.id !== effectiveCandidateId || pending.status === 'generating') && <p className="studio-clip-latest">{tr("最新尝试：")}{label(pending.status)}<button className="studio-text-button" onClick={() => { chooseCandidate(pending.id); chooseTab('history') }}>{tr("查看进展")}</button></p>}
      {clip.archived_at && <p className="studio-note">{tr("此片段计划已被调整，历史生成结果仍保留供追溯。")}</p>}
    </div>}</ClipPlayerFrame>
    <div className="studio-tabs studio-clip-tabs" role="tablist" aria-label={tr("视频详情内容")}>
      {tabs.map((item, index) => <button key={item.id} id={`${id}-${item.id}-tab`} role="tab" aria-selected={tab === item.id} aria-controls={`${id}-${item.id}-panel`} tabIndex={tab === item.id ? 0 : -1} onClick={() => chooseTab(item.id)} onKeyDown={(event) => {
        const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1
        if (next >= 0) { event.preventDefault(); chooseTab(tabs[next].id); document.getElementById(`${id}-${tabs[next].id}-tab`)?.focus() }
      }}>{item.label}</button>)}
    </div>
    <div ref={panel} className="studio-clip-tab-content" onPlayCapture={() => { const video = player.current?.querySelector('video'); if (video && !video.paused) video.pause() }}>
      {visited.has('overview') && <div role="tabpanel" id={`${id}-overview-panel`} aria-labelledby={`${id}-overview-tab`} hidden={tab !== 'overview'} tabIndex={0}>
        {validCandidate && <><CandidateReviewSummary key={effectiveCandidateId} candidate={validCandidate} compact /><button className="studio-text-button" onClick={() => chooseTab('checks')}>{tr("查看检查记录")}</button></>}
        <section><h3>{tr("本段剧情")}</h3><ReadingText text={clip.description} limit={180} lines={3} /></section>
        <ClipReadinessPanel project={project} clipId={clipId} onSelect={onSelect} />
        <ClipPreparationPanel project={project} clip={clip} />
        <LazyDetails title={tr("本段剧情状态与接续计划")}><ClipStatePlan project={project} clip={clip} /><button className="studio-text-button" onClick={() => onSelect({ kind: 'memory', id: clip.set_id, clipId })}>{tr("打开本段片场记忆")}</button></LazyDetails>
      </div>}
      {visited.has('references') && <div role="tabpanel" id={`${id}-references-panel`} aria-labelledby={`${id}-references-tab`} hidden={tab !== 'references'} tabIndex={0}>
        {candidates.length > 0 && <label className="studio-reference-version">{tr("参考素材版本")}<select aria-label={tr("参考素材的生成版本")} value={effectiveCandidateId ?? ''} onChange={(event) => chooseCandidate(event.target.value)}>{!effectiveCandidateId && <option value="" disabled>{tr("请选择生成版本")}</option>}{candidates.map((item) => <option key={item.id} value={item.id}>{attemptLabels[item.id]}{item.id === clip.accepted_candidate_id ? tr(" · 成片选用") : ''}</option>)}</select></label>}
        {history.controls}
        {validCandidate ? <CandidateReferences key={effectiveCandidateId} project={project} candidate={validCandidate} onSelect={onSelect} /> : <p className="studio-empty-line">{tr("选择一次生成后，可查看它实际使用的素材。")}</p>}
      </div>}
      {visited.has('checks') && <div role="tabpanel" id={`${id}-checks-panel`} aria-labelledby={`${id}-checks-tab`} hidden={tab !== 'checks'} tabIndex={0}>{validCandidate ? <><CandidateReviewSummary key={`summary:${effectiveCandidateId}`} candidate={validCandidate} /><CandidateReviewDetails key={`details:${effectiveCandidateId}`} project={project} candidate={validCandidate} /></> : <p className="studio-empty-line">{tr("选定一版视频后，在这里查看其检查结论。")}</p>}</div>}
      {visited.has('history') && <div role="tabpanel" id={`${id}-history-panel`} aria-labelledby={`${id}-history-tab`} hidden={tab !== 'history'} tabIndex={0}>
        <section className="studio-candidate-list"><h3>{tr("生成历史")}</h3>{candidates.map((item) => <button key={item.id} aria-pressed={item.id === effectiveCandidateId} onClick={() => chooseCandidate(item.id)}><span>{attemptLabels[item.id]}</span><Status value={item.status} />{item.id === clip.accepted_candidate_id && <strong>{tr("成片选用")}</strong>}</button>)}{history.controls}</section>
        {latestJob?.error && <LazyDetails title={tr("最新生成任务的失败说明")}><p className="studio-error-text"><EntityText>{latestJob.error}</EntityText></p></LazyDetails>}
        {clip.repair_reason && <LazyDetails title={tr("修复原因")}><Markdown>{clip.repair_reason}</Markdown></LazyDetails>}
        {validCandidate && <LazyDetails title={tr("本次生成的接续记录")}><CandidateContinuity project={project} candidate={validCandidate} onSelect={onSelect} /></LazyDetails>}
        {clip.reference_plan_id && <LazyDetails title={tr("参考清单变更记录")}><ReferencePlanPanel project={project} planId={clip.reference_plan_id} onSelect={onSelect} /></LazyDetails>}
        <QualityEpisodeDetail clip={clip} />
        <CandidateComparisonPanel project={project} clipId={clipId} onChoose={chooseCandidate} />
        {(clip.plan_parent_ids?.length ?? 0) > 0 && <LazyDetails title={tr("计划调整来源")}>{clip.plan_parent_ids?.map((parentId) => <button key={parentId} className="studio-usage" onClick={() => onSelect({ kind: 'clip', id: parentId })}>{project.clips?.[parentId]?.title ?? tr("原片段计划")}</button>)}</LazyDetails>}
      </div>}
    </div>
  </div></EntityLinkProvider>
}
