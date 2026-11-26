import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState, type RefObject } from 'react'
import { useEntityDetail, useStudioData } from './data'
import { EntityLink, EntityText } from './EntityLinks'
import { useStudioMediaUrl } from './dataContext'
import { roleLabels, type CandidateComparison, type ClipReadiness, type Selection, type StudioProject } from './model'
import { QualityProgressPanel } from './QualityProgress'
import './ProductionTools.css'

export function ClipReadinessPanel({ project, clipId, onSelect }: { project: StudioProject; clipId: string; onSelect: (value: Selection) => void }) {
  useLocale()

  const source = useStudioData()
  const [value, setValue] = useState<ClipReadiness>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const revision = source?.invalidations?.[`clip:${clipId}`]
  const memoryRevision = source?.invalidations?.['memory:global']
  useEffect(() => {
    if (!source?.client.readiness) return
    const controller = new AbortController()
    setError('')
    void source.client.readiness(source.projectId, clipId, controller.signal).then((result) => {
      if (!controller.signal.aborted) setValue(result)
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "制作准备信息加载失败") })
    return () => controller.abort()
  }, [source?.client, project.id, clipId, revision, memoryRevision, retry])
  const progress = value?.clip_id === clipId ? value.quality_progress ?? project.clips?.[clipId]?.quality_progress : project.clips?.[clipId]?.quality_progress
  const showReadiness = source?.client.readiness && !project.clips?.[clipId]?.accepted_candidate_id
  return <>{progress && <QualityProgressPanel project={project} progress={progress} onSelect={onSelect} />}{showReadiness && <section className="studio-readiness" aria-label={tr("制作准备情况")}><h3>{tr("制作准备")}</h3>
    {value && value.clip_id === clipId && <p className="studio-readiness-stages"><span>{value.can_plan ? tr("可以继续规划") : tr("等待片场交接")}</span><strong>{value.can_generate ? tr("允许提交生成") : tr("生成尚有前置条件")}</strong></p>}
    {error ? <p role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((n) => n + 1)}>{tr("重试")}</button></p>
      : !value || value.clip_id !== clipId ? <p role="status">{tr("正在核对准备情况…")}</p>
        : value.ready ? <p>{tr("所需内容已就绪，导演可继续处理。")}</p>
          : <ul>{value.items?.map((item) => <li key={item.key}><EntityText>{trSystem(item.description)}</EntityText>{Object.keys(item.details ?? {}).length > 0 && <ConflictDiagnostic details={item.details ?? {}} />}<div className="studio-readiness-owner"><button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: item.owner_id })}>{tr("{{0}}负责", roleLabels[item.owner_role])}</button>{item.proposal_ids?.map((id) => <EntityLink key={id} id={id}>{tr("查看已有提案")}</EntityLink>)}</div></li>)}</ul>}
  </section>}</>
}

function ConflictDiagnostic({ details }: { details: Record<string, unknown> }) {
  useLocale()

  const items = Array.isArray(details.conflicts) ? details.conflicts : [details]
  return <div className="studio-state-conflicts">{items.map((raw, index) => {
    const item = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
    const states = Array.isArray(item.matched_states) ? item.matched_states as Record<string, unknown>[] : []
    if (!item.asset_id && !states.length) return null
    return <dl key={index}><dt>{tr("资产")}</dt><dd>{String(item.asset_name ?? item.asset_id)}</dd>{item.expected_state_id ? <><dt>{tr("计划状态")}</dt><dd><EntityLink id={String(item.expected_state_id)}>{item.expected_state_label ? String(item.expected_state_label) : undefined}</EntityLink></dd></> : null}{item.story_anchor ? <><dt>{tr("本段剧情位置")}</dt><dd><EventAnchor value={item.story_anchor} /></dd></> : null}{states.length > 0 && <><dt>{tr("实际匹配状态")}</dt><dd>{states.map((state, i) => <div key={i}><EntityLink id={String(state.state_id)}>{String(state.label ?? state.state_id)}</EntityLink><p className="studio-note"><EventAnchor value={state.starts_at} fallback={tr("无起点限制")} /> — <EventAnchor value={state.ends_at} fallback={tr("无终点限制")} /></p></div>)}</dd></>}</dl>
  })}</div>
}

function EventAnchor({ value, fallback = tr("未记录") }: { value: unknown; fallback?: string }) {
  useLocale()

  const anchor = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  return anchor.event_id ? <><EntityLink id={String(anchor.event_id)} />{anchor.position === 'before' ? tr("之前") : tr("之后")}</> : <>{fallback}</>
}

function ExactVideo({ project, candidateId, videoRef, muted, independent, onTime, onDuration, onPause, onEnded, onError }: {
  project: StudioProject; candidateId: string; videoRef: RefObject<HTMLVideoElement>; muted: boolean; independent: boolean;
  onTime: () => void; onDuration: () => void; onPause: () => void; onEnded: () => void; onError: () => void;
}) {
  useLocale()

  const mediaUrl = useStudioMediaUrl()
  const detail = useEntityDetail(project, `candidate:${candidateId}`)
  const candidate = detail.project.candidates?.[candidateId]
  const media = candidate?.media_id ? detail.project.media?.[candidate.media_id] : undefined
  return <>{detail.status}{media ? <video ref={videoRef} src={mediaUrl(media.url)} preload="metadata" playsInline controls={independent} muted={muted} onTimeUpdate={onTime} onLoadedMetadata={onDuration} onDurationChange={onDuration} onPause={onPause} onEnded={onEnded} onError={onError} aria-label={tr("候选视频对照")} /> : !detail.loading && <p className="studio-note">{tr("这版视频暂不可用，保留精确候选，不替换成其他版本。")}</p>}</>
}

const seconds = (value: number) => `${Math.floor(value / 60)}:${Math.floor(value % 60).toString().padStart(2, '0')}`

export function PairedVideos({ project, comparison }: { project: StudioProject; comparison: CandidateComparison }) {
  useLocale()

  const playable = comparison.candidates?.filter((candidate) => candidate.media_id) ?? []
  const fallback = comparison.default_pair ?? playable.slice(-2).map((candidate) => candidate.id)
  const [chosen, setChosen] = useState<string[]>(fallback)
  const first = useRef<HTMLVideoElement>(null)
  const second = useRef<HTMLVideoElement>(null)
  const [linked, setLinked] = useState(true)
  const [audible, setAudible] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [duration, setDuration] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const startingPlayback = useRef<symbol>()
  const pair = chosen.length === 2 && chosen.every((id) => playable.some((c) => c.id === id)) ? chosen : fallback
  const videos = () => [first.current, second.current].filter((v): v is HTMLVideoElement => Boolean(v))
  const pause = () => {
    startingPlayback.current = undefined
    videos().forEach((video) => { if (!video.paused) video.pause() })
    setStarting(false); setPlaying(false)
  }
  useEffect(() => {
    const targets = [first.current, second.current]
    startingPlayback.current = undefined
    targets.forEach((video) => video?.pause())
    setDuration(0); setElapsed(0); setStarting(false); setPlaying(false); setError('')
    return () => { startingPlayback.current = undefined; targets.forEach((video) => video?.pause()) }
  }, [pair[0], pair[1]])
  const measure = () => {
    const values = videos().map((video) => video.duration)
    setDuration(values.length === 2 && values.every((n) => Number.isFinite(n) && n > 0) ? Math.min(...values) : 0)
  }
  const seek = (time: number) => {
    const value = Math.max(0, Math.min(duration, time))
    videos().forEach((video) => { if (Number.isFinite(video.duration)) video.currentTime = Math.min(value, video.duration) })
    setElapsed(value)
  }
  const tick = (side: number) => {
    if (!linked || side !== 0 || !first.current) return
    const time = first.current.currentTime
    if (duration && time >= duration - .04) { pause(); seek(duration); return }
    setElapsed(time)
    if (second.current && Math.abs(second.current.currentTime - time) > .15) second.current.currentTime = Math.min(time, duration || time)
  }
  const play = async () => {
    if (playing) { pause(); return }
    if (!duration || startingPlayback.current) return
    const attempt = Symbol()
    startingPlayback.current = attempt
    setError(''); setStarting(true)
    seek(elapsed >= duration - .04 ? 0 : elapsed)
    const results = await Promise.allSettled(videos().map(async (video) => video.play()))
    // Pause, unlink or a new pair cancels this start. The old response never
    // stops or advances a later attempt on any of the same video elements.
    if (startingPlayback.current !== attempt) return
    startingPlayback.current = undefined
    setStarting(false)
    if (results.some((result) => result.status === 'rejected')) {
      pause(); setError("视频暂时无法播放，请重试或查看对应成果。")
      return
    }
    setPlaying(true)
  }
  const replace = (side: number, id: string) => {
    pause(); setDuration(0); setElapsed(0)
    const next = [...pair]; next[side] = id
    setChosen(next)
  }
  if (playable.length < 2 || pair.length < 2) return <p className="studio-note">{tr("有两版可播放的视频后，可以在这里并排对照。")}</p>
  return <section className="studio-paired-videos" aria-label={tr("两版视频对照")}>
    <div className="studio-paired-heading"><h4>{tr("两版视频对照")}</h4><label><input type="checkbox" checked={linked} onChange={(event) => { pause(); setLinked(event.target.checked); if (event.target.checked) seek(0) }} />{tr("联动播放")}</label><label>{tr("声音")}<select aria-label={tr("对照声音来源")} value={audible} onChange={(event) => setAudible(Number(event.target.value))}><option value={0}>{tr("左侧")}</option><option value={1}>{tr("右侧")}</option></select></label></div>
    <div className="studio-paired-grid">{pair.map((id, side) => <div className="studio-paired-side" key={side}><label>{side === 0 ? tr("左侧候选") : tr("右侧候选")}<select aria-label={side === 0 ? tr("左侧候选") : tr("右侧候选")} value={id} onChange={(event) => replace(side, event.target.value)}>{playable.map((candidate) => <option key={candidate.id} value={candidate.id} disabled={candidate.id === pair[1 - side]}>{tr("第 {{0}} 次", candidate.attempt)}{candidate.selected ? tr(" · 已选用") : ''}</option>)}</select></label>
      <ExactVideo key={id} project={project} candidateId={id} videoRef={side === 0 ? first : second} muted={audible !== side} independent={!linked} onTime={() => tick(side)} onDuration={measure} onPause={() => { if (linked) pause() }} onEnded={() => { if (linked) { pause(); seek(duration) } }} onError={() => { pause(); setDuration(0); setError("这版视频加载失败，请打开对应成果检查文件。") }} />
      <EntityLink id={id}>{tr("打开这版成果与检查")}</EntityLink></div>)}</div>
    {linked && <div className="studio-paired-controls"><button className="studio-text-button" disabled={!duration || starting} onClick={() => void play()}>{playing ? tr("一起暂停") : starting ? tr("正在开始…") : tr("一起播放")}</button><input type="range" aria-label={tr("联动播放进度")} min={0} max={duration || 1} step={0.01} value={Math.min(elapsed, duration || 1)} disabled={!duration} onChange={(event) => seek(Number(event.target.value))} /><span>{seconds(elapsed)} / {seconds(duration)}</span></div>}
    {error && <p role="alert">{trSystem(error)}</p>}<p className="studio-note">{tr("联动时按相同经过秒数对照，较短一段结束后一起暂停。")}</p>
  </section>
}

export function CandidateComparisonPanel({ project, clipId, onChoose }: { project: StudioProject; clipId: string; onChoose: (id: string) => void }) {
  useLocale()

  const source = useStudioData()
  const [comparison, setComparison] = useState<CandidateComparison>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const invalidation = source?.invalidations?.[`clip:${clipId}`]
  useEffect(() => {
    if (!source?.client.comparison) return
    const controller = new AbortController()
    setError('')
    void (async () => {
      let page = await source.client.comparison!(source.projectId, clipId, undefined, controller.signal)
      const seen = new Set<string>()
      while (page.next_cursor && !controller.signal.aborted) {
        if (seen.has(page.next_cursor)) throw new Error("候选分页未前进，请重试。")
        seen.add(page.next_cursor)
        const next = await source.client.comparison!(source.projectId, clipId, page.next_cursor, controller.signal)
        page = { ...next, candidates: [...(page.candidates ?? []), ...(next.candidates ?? [])] }
      }
      if (!controller.signal.aborted) setComparison(page)
    })().catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "候选对比加载失败") })
    return () => controller.abort()
  }, [source?.client, project.id, clipId, invalidation, retry])
  if (!source?.client.comparison) return null
  if (error) return <p role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((n) => n + 1)}>{tr("重试")}</button></p>
  if (!comparison || comparison.clip_id !== clipId) return <p role="status">{tr("正在整理候选对比…")}</p>
  return <section aria-label={tr("候选对比")}>
    <div className="studio-quality-heading"><h3>{tr("候选对比")}</h3></div>
    <div className="studio-comparison-table" role="region" aria-label={tr("全部候选结论")} tabIndex={0}>
      <table>
        <thead><tr><th scope="col">{tr("生成候选")}</th><th scope="col">{tr("检查结论")}</th><th scope="col">{tr("选用情况")}</th></tr></thead>
        <tbody>{comparison.candidates?.map((candidate) => <tr key={candidate.id}>
          <th scope="row"><button className="studio-text-button" onClick={() => onChoose(candidate.id)}>{tr("第 {{0}} 次生成", candidate.attempt)}</button></th>
          <td>{candidate.review?.decision === 'accept' ? tr("检查通过") : candidate.review?.decision === 'reject' ? tr("检查未通过") : tr("尚无检查结论")}</td>
          <td>{candidate.selected ? candidate.selection?.kind === 'system_fallback' ? tr("系统兜底选用") : candidate.selection?.kind === 'director_compromise' ? tr("妥协选用") : candidate.selection?.kind === 'user_selection' ? tr("临时选用") : tr("已选用") : tr("未选用")}</td>
        </tr>)}</tbody>
      </table>
    </div>
    <p className="studio-note">{tr("点击候选可查看导演判断、问题与画面证据。")}</p>
    <PairedVideos key={clipId} project={project} comparison={comparison} />
  </section>
}
