import { tr, useLocale } from '../../ui/locale'
import type { Candidate, Selection, StudioProject } from './model'
import { referenceName, viewLabel } from './model'
import { CandidateMedia } from './CandidateMedia'
import { Markdown } from './Markdown'
import { LazyDetails } from './data'
import { candidateImageInputs, candidateSubmission, records, textValue, seconds } from './candidate-details'
import { SixSectionPrompt } from './SixSectionPrompt'
import './CandidateDetails.css'

const timedRoleLabels: Record<string, string> = { get continuation() { return tr("前段尾部接续") }, get voice() { return tr("固定音色") }, get ambience() { return tr("环境声") }, get sound() { return tr("声音参考") }, get motion() { return tr("动作参考") }, get identity() { return tr("身份参考") }, get edit_source() { return tr("历史编辑来源") } }

export function CandidateReferences({ project, candidate, onSelect }: { project: StudioProject; candidate: Candidate; onSelect: (selection: Selection) => void }) {
  useLocale()

  const inputs = candidateImageInputs(project, candidate)
  const submission = candidateSubmission(project, candidate)
  const textOnly = submission.video_input_policy === 'text-only'
  const timed = (['video_inputs', 'audio_inputs'] as const).flatMap((kind) => records(submission[kind]).map((input, index) => ({ input, label: `${kind === 'video_inputs' ? 'Video' : 'Audio'} ${index + 1}` })))
  return <section className="studio-candidate-references" aria-label={textOnly ? tr("本地规划与审片参考") : tr("本次生成的全部参考素材")}>
    {textOnly && <p className="studio-review-coverage">{tr("视频 API 仅收到文字。以下素材保留用于本地规划和审核。")}</p>}
    {!textOnly && records(submission.reference_inspection_gaps).length > 0 && <p className="studio-review-coverage">{tr("本次使用了未完成审看的前段真实尾帧。末态描述为暂定，接缝效果尚未验证。")}</p>}
    <div className="studio-quality-heading"><h3>{textOnly ? tr("本地参考图片") : tr("本次参考图片")}</h3><span className="studio-secondary">{inputs.length} {tr("张")}</span></div>
    {inputs.length ? <div className="studio-input-grid">{inputs.map((input) => {
      const revision = project.references?.[input.revisionId]
      const title = tr("图片 {{0}} · {{1}}{{2}}{{3}}", input.number, input.assetName, input.view ? ` · ${viewLabel(input.view)}` : '', input.selection === 'crop' ? tr("（实际裁剪图）") : '')
      return <article className="studio-input-card studio-input-card--image" key={`${candidate.id}:${input.number}`} aria-label={title}>
        <header><span className="studio-input-number">{tr("图片")} {input.number}</span>{input.exact && <small>Picture {input.number}</small>}<strong title={input.assetName}>{input.assetName}</strong></header>
        <div className="studio-input-image">{input.mediaId ? <CandidateMedia key={input.mediaId} project={project} mediaId={input.mediaId} title={title} /> : <p className="studio-media-empty">{input.exact ? tr("当次图片记录缺失") : tr("当次提交图片未记录")}</p>}</div>
        <div className="studio-input-body">
          {!input.boundary && <><p className="studio-input-state" title={input.stateLabel}>{input.stateLabel}</p><p className="studio-note">{input.view ? viewLabel(input.view) : tr("视图未记录")}{input.selection === 'crop' ? tr(" · 裁剪单图") : input.selection === 'full_board' ? tr(" · 完整参考板") : ''}</p></>}
          {input.revisionId && <button className="studio-text-button" title={revision ? referenceName(revision) : tr("查看绑定修订")} onClick={() => onSelect({ kind: 'asset', id: input.assetId, stateId: input.stateId, referenceId: input.revisionId })}>{input.selection === 'crop' ? tr("查看来源原图") : tr("查看该版图片")}</button>}
          {input.boundary && candidate.previous_candidate_id && <button className="studio-text-button" onClick={() => onSelect({ kind: 'entity', id: candidate.previous_candidate_id! })}>{tr("查看上一片段来源")}</button>}
          {input.reason && <LazyDetails title={tr("参考选择说明")}><Markdown>{input.reason}</Markdown></LazyDetails>}
        </div>
      </article>
    })}</div> : <p className="studio-empty-line">{tr("尚无参考图片记录。")}</p>}
    {timed.length > 0 && <section className="studio-timed-references" aria-label={tr("本次视频与音频参考")}><h3>{tr("视频与音频参考")}</h3><div className="studio-input-grid">{timed.map(({ input, label }) => <article className="studio-input-card" key={label}><header><strong>{label}</strong><span>{timedRoleLabels[textValue(input.role)] ?? tr("当次参考")}</span></header><div className="studio-input-body">
      <CandidateMedia project={project} mediaId={textValue(input.media_id)} title={`${label} · ${input.role === 'continuation' ? tr("上一片段尾部 {{0}}", seconds(input.duration)) : tr("当次实际参考")}`} />
      <p className="studio-note">{seconds(input.duration)} {tr("· 来源")} {seconds(input.start_seconds) || tr("未记录")} — {seconds(input.end_seconds) || tr("未记录")}</p>
      {input.source_candidate_id ? <button className="studio-text-button" onClick={() => onSelect({ kind: 'entity', id: textValue(input.source_candidate_id) })}>{tr("查看来源视频")}</button> : null}
    </div></article>)}</div></section>}
    {textOnly ? <details className="studio-six-prompt"><summary>{tr("视频 API 提示词")}</summary><pre>{submission.prompt}</pre></details> : <SixSectionPrompt prompt={submission.prompt} />}
  </section>
}
