import { tr, trSystem, useLocale } from '../../ui/locale'
import { useState } from 'react'
import { Download } from 'lucide-react'
import type { Candidate, ReviewGap, StudioProject } from './model'
import { EntityText } from './EntityLinks'
import { Markdown } from './Markdown'
import { MediaPreview } from './Media'
import { CandidateMedia } from './CandidateMedia'
import { LazyDetails, useEntityDetail } from './data'
import { ReadingText } from './ReadingText'
import { candidateVerdict, evidenceGroups, originalReviews, record, recordedTime, records, seconds, selectionKind, strings, textValue, unique, type EvidenceGroup } from './candidate-details'
import './CandidateDetails.css'

export function CandidateReviewSummary({ candidate, compact = false }: { candidate: Candidate; compact?: boolean }) {
  useLocale()

  const gaps = candidate.review?.coverage_gaps ?? candidate.review_gaps ?? []
  const kind = selectionKind(candidate)
  const special = kind === 'user_selection' || kind === 'director_compromise' || kind === 'system_fallback'
  const legacy = record(candidate.analysis?.user_selection)
  const reason = special ? candidate.selection?.reason || textValue(legacy.reason) || textValue(legacy.user_request) : candidate.review?.reason
  const readableReason = reason?.replace(/\bgeneration_attempt\s*=\s*(\d+)\b/g, tr("第 $1 次生成"))
  const issues = unique([...(candidate.issues ?? []), ...(candidate.selection?.deviations ?? []), ...strings(legacy.unresolved_issues)])
  const rejected = originalReviews(candidate).some((item) => item.decision === 'reject')
  return <section className={`studio-review-overview${special || candidate.review?.decision === 'reject' ? ' has-caveat' : ''}`} aria-label={tr("本次视频的检查结论")}>
    <h3>{tr("检查结论")}</h3><strong className="studio-review-verdict">{candidateVerdict(candidate)}</strong>
    {gaps.length > 0 && <p className="studio-review-coverage" role="note">{candidate.review ? tr("有限审核") : tr("审核范围受限")}：{gaps.length} {tr("张图片已隔离，未审画面不计为通过。原素材保留。")}</p>}
    {special && <p className="studio-note">{rejected ? tr("原导演检查未通过；") : ''}{tr("本次选用不代表检查通过，原始判断与偏差均保留。")}</p>}
    {issues.length > 0 && <div className="studio-review-attention"><h4>{special ? tr("选用时保留的问题") : tr("需要处理的问题")}{compact ? tr(" · {{0}} 项", issues.length) : ''}</h4>{!compact && <ul>{issues.map((issue) => <li key={issue}><ReadingText text={issue} limit={180} label={tr("问题说明")} /></li>)}</ul>}</div>}
    {kind === 'user_selection' && textValue(legacy.user_request) && <p className="studio-note">{tr("用户要求：")}<EntityText>{textValue(legacy.user_request)}</EntityText></p>}
    {readableReason && (!compact || special) && <ReadingText key={`${candidate.id}:${kind}`} text={readableReason} limit={180} lines={3} label={special ? tr("选用说明") : tr("导演说明")} />}
    {!compact && reason && readableReason !== reason && <LazyDetails title={tr("选用说明原文")}><Markdown>{reason}</Markdown></LazyDetails>}
    {!candidate.review && !special && <p className="studio-note">{candidate.status === 'failed' ? tr("本次生成未完成，尚无可用的验收结论。") : tr("等待导演查看实际画面后给出验收结论。")}</p>}
  </section>
}

const categoryNames: Record<string, string> = { get identity() { return tr("人物与身份") }, get state() { return tr("剧情状态") }, get action() { return tr("动作") }, get space() { return tr("空间") }, get camera() { return tr("机位") }, get visual_quality() { return tr("画面质量") }, get other() { return tr("其他") } }

function DirectorFindings({ project, review }: { project: StudioProject; review?: Candidate['review'] }) {
  useLocale()

  return <div className="studio-review-issues" aria-label={tr("片场导演自审问题")}>{review?.findings?.map((finding, index) => <article key={index}>
    <header><strong>{categoryNames[finding.category ?? 'other']} · {seconds(finding.start_seconds)}{finding.end_seconds != null ? ` — ${seconds(finding.end_seconds)}` : ''}</strong><span data-conclusion={finding.severity === 'repair' ? 'confirmed' : 'minor'}>{finding.severity === 'repair' ? tr("需要修正") : tr("轻微问题，已记录")}</span></header>
    <Markdown>{finding.description}</Markdown><LazyDetails title={tr("查看问题画面证据")}><EvidenceGallery project={project} groups={[{ id: 'finding', title: tr("自审画面依据"), mediaIds: finding.evidence_media_ids }]} /></LazyDetails>
  </article>)}</div>
}

function SamplingSummary({ analysis }: { analysis: Record<string, unknown> }) {
  useLocale()

  const sampling = record(analysis.sampling)
  const timestamps = Array.isArray(analysis.sampled_frame_timestamps) ? analysis.sampled_frame_timestamps : []
  const count = timestamps.length || (Array.isArray(analysis.sampled_frame_indices) ? analysis.sampled_frame_indices.length : 0)
  const layout = record(analysis.evidence_layout)
  const description = [typeof sampling.fps === 'number' ? tr("整段每秒 {{0}} 帧", sampling.fps) : '', typeof sampling.boundary_fps === 'number' ? tr("首尾每秒 {{0}} 帧", sampling.boundary_fps) : '', count ? tr("本段共 {{0}} 帧", count) : '', layout.frames_per_sheet === 4 ? tr("每张拼图 4 帧") : ''].filter(Boolean)
  const intervals = records(sampling.focus_intervals)
  return <div className="studio-sampling-summary">{description.length > 0 && <p className="studio-note">{description.join(' · ')}</p>}{intervals.length > 0 && <p className="studio-note">{tr("重点复查：")}{intervals.map((interval) => `${seconds(interval.start_seconds)} — ${seconds(interval.end_seconds)}${typeof interval.fps === 'number' ? tr("（每秒 {{0}} 帧）", interval.fps) : ''}`).join('；')}</p>}</div>
}

export function EvidenceGallery({ project, groups }: { project: StudioProject; groups: EvidenceGroup[] }) {
  useLocale()

  const [chosen, setChosen] = useState('')
  const [page, setPage] = useState(0)
  const group = groups.find((item) => item.id === chosen) ?? groups[0]
  if (!group) return <p className="studio-empty-line">{tr("暂无检查画面。")}</p>
  const index = Math.min(page, group.mediaIds.length - 1)
  const mediaId = group.mediaIds[index]
  return <div className="studio-evidence-gallery">
    {groups.length > 1 && <label className="studio-evidence-select">{tr("画面类型")}<select aria-label={tr("检查画面类型")} value={group.id} onChange={(event) => { setChosen(event.target.value); setPage(0) }}>{groups.map((item) => <option key={item.id} value={item.id}>{item.title} · {tr("{{0}} 页", item.mediaIds.length)}</option>)}</select></label>}
    <div className="studio-evidence-pagination"><strong>{group.title}</strong><span>{tr("第 {{0}} / {{1}} 页", index + 1, group.mediaIds.length)}</span>{group.mediaIds.length > 1 && <><button className="studio-text-button" disabled={index === 0} onClick={() => setPage(index - 1)}>{tr("上一页画面")}</button><button className="studio-text-button" disabled={index === group.mediaIds.length - 1} onClick={() => setPage(index + 1)}>{tr("下一页画面")}</button></>}</div>
    <CandidateMedia key={mediaId} project={project} mediaId={mediaId} title={tr("{{0}} · 第 {{1}} 页", group.title, index + 1)} />
  </div>
}

function AnalysisJobRecord({ project, candidate, jobId }: { project: StudioProject; candidate: Candidate; jobId: string }) {
  useLocale()

  const detail = useEntityDetail(project, `job:${jobId}`)
  const job = detail.project.jobs?.[jobId]
  if (job && (job.kind !== 'analysis' || job.target_id !== candidate.id)) return <p role="alert">{tr("这条记录不属于当前视频的检查，无法展示。")}</p>
  const analysis = record(record(job?.result).analysis)
  const groups = evidenceGroups(analysis, textValue(record(record(job?.result).end_state).frame_media_id))
  return <div className="studio-prior-analysis">{detail.status}{job && <p className="studio-note">{recordedTime(job.created_at)}</p>}{job?.error && <p role="alert"><EntityText>{job.error}</EntityText></p>}
    {Object.keys(analysis).length > 0 ? <><p className="studio-note">{tr("这次提取的画面用于导演审核，结论保存在对应的检查记录中。")}</p><LazyDetails title={tr("查看这次检查的画面")}><SamplingSummary analysis={analysis} /><EvidenceGallery project={detail.project} groups={groups} /></LazyDetails></> : !detail.loading && !detail.error && <p className="studio-empty-line">{tr("这份历史分析暂无可展示的结果。")}</p>}
  </div>
}

function downloadReview(candidate: Candidate) {
  const archive = { analysis: candidate.analysis, review: candidate.review, review_batches: candidate.review_batches, review_history: originalReviews(candidate), selection: candidate.selection, issues: candidate.issues, end_state: candidate.end_state, confirmed_end_state: candidate.confirmed_end_state, end_state_history: candidate.end_state_history }
  const url = URL.createObjectURL(new Blob([JSON.stringify(archive, null, 2)], { type: 'application/json' }))
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = tr("视频检查记录.json"); anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function CandidateReviewDetails({ project, candidate }: { project: StudioProject; candidate: Candidate }) {
  useLocale()

  const analysis = record(candidate.analysis)
  const reviews = originalReviews(candidate)
  const priorIds = unique(strings(analysis.prior_analysis_job_ids))
  const groups = evidenceGroups(analysis, candidate.end_state?.frame_media_id)
  const finalState = candidate.confirmed_end_state?.end_state ?? candidate.end_state
  const hasDetails = Object.keys(analysis).length > 0 || !!candidate.review || !!candidate.end_state
  const limitations = unique([...(candidate.review?.limitations ?? []), ...strings(analysis.limitations)])
  if (!hasDetails) return null
  return <section className="studio-review-details" aria-label={tr("视频检查详情")}>
    <h3>{candidate.review?.mode === 'external_operator' ? tr("外部人工检查") : tr("片场导演自审")}</h3>
    <CoverageGaps project={project} gaps={candidate.review?.coverage_gaps ?? candidate.review_gaps ?? []} />
    {!!candidate.review_batches?.length && <ReviewBatches project={project} candidate={candidate} />}
    <p className="studio-note">{tr("根据实际查看的画面给出结论、问题与证据。")}</p><DirectorFindings project={project} review={candidate.review} />{candidate.review && !(candidate.review.findings?.length) && <p className="studio-note">{tr("本次自审未记录具体画面问题。")}</p>}{limitations.length > 0 && <LazyDetails title={tr("本次检查的范围与限制")}><ul>{limitations.map((item) => <li key={item}>{item}</li>)}</ul></LazyDetails>}
    {groups.length > 0 && <LazyDetails title={tr("检查画面（{{0}} 页）", groups.reduce((sum, group) => sum + group.mediaIds.length, 0))}><SamplingSummary analysis={analysis} /><EvidenceGallery key={candidate.id} project={project} groups={groups} /></LazyDetails>}
    {finalState && <LazyDetails title={candidate.confirmed_end_state ? tr("导演确认的接续状态") : tr("片段结束状态")}>{candidate.confirmed_end_state && <ReadingText text={candidate.confirmed_end_state.reason} limit={180} label={tr("确认依据")} />}<dl className="studio-end-state">{(['identity', 'action', 'space', 'camera'] as const).map((key) => <div key={key}><dt>{{ identity: tr("人物"), action: tr("动作"), space: tr("空间"), camera: tr("机位") }[key]}</dt><dd><EntityText>{finalState[key]}</EntityText></dd></div>)}</dl></LazyDetails>}
    {(reviews.length > 1 || priorIds.length > 0 || selectionKind(candidate) === 'user_selection' || selectionKind(candidate) === 'director_compromise' || selectionKind(candidate) === 'system_fallback') && <LazyDetails title={tr("历史检查与原始判断")}>
      {reviews.map((review, index) => <LazyDetails key={`${review.created_at}:${index}`} title={tr("导演检查{{0}}{{1}}", review.decision === 'accept' ? tr("通过") : tr("未通过"), recordedTime(review.created_at) ? ` · ${recordedTime(review.created_at)}` : '')}><Markdown>{review.reason}</Markdown><DirectorFindings project={project} review={review} />{review.evidence_media_ids?.length ? <LazyDetails title={tr("查看当时自审的画面")}><HistoricalEvidence project={project} mediaIds={review.evidence_media_ids} /></LazyDetails> : null}</LazyDetails>)}
      {priorIds.map((jobId, index) => <LazyDetails key={jobId} title={tr("较早的画面证据 · 记录 {{0}}", index + 1)}><AnalysisJobRecord project={project} candidate={candidate} jobId={jobId} /></LazyDetails>)}
    </LazyDetails>}
    <button className="studio-text-button studio-review-download" onClick={() => downloadReview(candidate)}><Download size={15} />{tr("下载本次检查记录")}</button>
  </section>
}

function CoverageGaps({ project, gaps }: { project: StudioProject; gaps: ReviewGap[] }) {
  useLocale()

  if (!gaps.length) return null
  return <section className="studio-review-coverage" aria-label={tr("未审核的画面范围")}>
    <h4>{tr("未审核的画面范围")}</h4>
    <p>{tr("上游拒绝了一批图片，未指出具体触发图片。以下图片已从导演输入隔离，结论仅依据其余证据。")}</p>
    <ul>{gaps.map((gap, index) => <li key={gap.media_id}>{gap.kind === 'previous_tail' ? tr("前段尾部") : gap.kind === 'seam' ? tr("接缝对照") : gap.kind === 'end_frame' ? tr("实际末帧") : tr("图片")} {index + 1}{gap.timestamps?.length ? ` · ${gap.kind === 'previous_tail' ? tr("前段 ") : ''}${seconds(Math.min(...gap.timestamps))} — ${seconds(Math.max(...gap.timestamps))}` : tr(" · 时间范围未记录")}<span>{trSystem(gap.reason)}</span></li>)}</ul>
    <LazyDetails title={tr("查看保留的原始图片")}><HistoricalEvidence project={project} mediaIds={gaps.map((gap) => gap.media_id)} /></LazyDetails>
  </section>
}

function ReviewBatches({ project, candidate }: { project: StudioProject; candidate: Candidate }) {
  useLocale()

  const current = candidate.review_batches?.filter((batch) => batch.analysis_job_id === candidate.analysis?.request_id) ?? []
  const completed = current.filter((batch) => batch.status === 'complete').length
  return <div aria-label={tr("分批审核记录")}><p className="studio-note">{tr("已完成")} {completed} / {current.length} {tr("批审核 · 每批最多 6 张证据图")}</p>
    <LazyDetails title={tr("查看各批结论")}>{current.map((batch, index) => <LazyDetails key={batch.id} title={tr("第 {{0}} 批 · {{1}} · {{2}}", index + 1, batch.purpose === 'references' ? tr("参考对照") : batch.purpose === 'followup' ? tr("局部复查") : tr("画面检查"), batch.status === 'complete' ? tr("已完成") : tr("待检查"))}>
      <p>{batch.question}</p>{batch.reason && <Markdown>{batch.reason}</Markdown>}
      {batch.findings?.map((finding, findingIndex) => <p key={findingIndex}><strong>{finding.severity === 'repair' ? tr("需要修正") : tr("轻微问题")}{tr("：")}</strong>{finding.description}</p>)}
      {batch.limitations?.map((item) => <p className="studio-note" key={item}>{item}</p>)}
      <LazyDetails title={tr("查看本批画面")}><EvidenceGallery project={project} groups={[{ id: 'batch', title: tr("第 {{0}} 批", index + 1), mediaIds: batch.media_ids }]} /></LazyDetails>
    </LazyDetails>)}</LazyDetails>
  </div>
}

function HistoricalEvidence({ project, mediaIds }: { project: StudioProject; mediaIds: string[] }) {
  useLocale()

  const [index, setIndex] = useState(0)
  const mediaId = mediaIds[Math.min(index, mediaIds.length - 1)]
  const detail = useEntityDetail(project, mediaId && !project.media?.[mediaId] ? `media:${mediaId}` : undefined)
  return <>{detail.status}<div className="studio-evidence-pagination"><span>{tr("第 {{0}} / {{1}} 页", index + 1, mediaIds.length)}</span><button className="studio-text-button" disabled={index === 0} onClick={() => setIndex(index - 1)}>{tr("上一页画面")}</button><button className="studio-text-button" disabled={index >= mediaIds.length - 1} onClick={() => setIndex(index + 1)}>{tr("下一页画面")}</button></div><MediaPreview key={mediaId} media={detail.project.media?.[mediaId]} title={tr("当时判断依据 · 第 {{0}} 页", index + 1)} /></>
}
