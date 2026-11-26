import { tr, useLocale } from '../../ui/locale'
import { useEntityDetail, useEntityHistory, LazyDetails } from './data'
import { EntityLink, EntityLinkProvider } from './EntityLinks'
import { MediaPreview } from './Media'
import { Markdown } from './Markdown'
import { type Selection, type StudioProject, referenceName, viewLabel } from './model'
import './StoryMemory.css'

const planLabels = { get draft() { return tr("待导演核对") }, get finalized() { return tr("已定稿，可提交") }, get needs_review() { return tr("相关内容已变化，需重核") }, get submitted() { return tr("已提交，输入已固定") }, get superseded() { return tr("已有新版") }, get legacy() { return tr("历史生成输入") } }

export function ReferencePlanPanel({ project: initialProject, planId, onSelect }: { project: StudioProject; planId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const detail = useEntityDetail(initialProject, `reference-plan:${planId}`)
  const project = detail.project
  const plan = project.reference_plans?.[planId]
  const clip = plan ? project.clips?.[plan.clip_id] : undefined
  const boundary = plan?.previous_end_state
  const candidateId = plan?.job_id ? project.jobs?.[plan.job_id]?.target_id : undefined
  const candidate = candidateId ? project.candidates?.[candidateId] : undefined
  return <EntityLinkProvider project={project} onSelect={onSelect}><section className="studio-reference-plan" aria-label={tr("生成前参考清单")}>
    {detail.status}{plan ? <>
      <div className="reference-plan-status"><h3>{clip?.title ?? tr("当前片段")} {tr("· 参考清单")}</h3><span>{tr("第 {{0}} 版", plan.revision)}</span><strong>{planLabels[plan.status ?? 'draft']}</strong></div>
      <p className="studio-note">{tr("具体素材和用途由导演维护。这里可以查看原图、片段区间和版本，修改由导演完成。")}</p>
      {candidate && <p className="studio-note">{candidate.id === clip?.accepted_candidate_id ? tr("这份清单用于当前已选视频。") : clip?.accepted_candidate_id ? tr("当前选用的是另一版视频；这里保留这次生成的原始输入。") : tr("这次生成尚未选用。")}<EntityLink id={candidateId!}>{tr("查看对应视频与验收")}</EntityLink></p>}
      {plan.invalidation_reasons?.length ? <div className="reference-plan-warning">{plan.invalidation_reasons.map((text) => <p key={text}>{text}</p>)}</div> : null}
      {plan.inspection_gaps?.length ? <p className="reference-plan-warning">{tr("前段真实尾帧作为接续输入保留，但未完成审看。末态描述为暂定，接缝效果尚未验证。")}</p> : null}
      <div className="reference-plan-list">
        {boundary && <article><h4>{tr("Picture 1 · 前段实际末帧")}</h4><MediaPreview media={project.media?.[boundary.frame_media_id]} title={tr("前段实际末帧")} /><p>{tr("延续：")}{boundary.action}</p>{plan.previous_candidate_id && <EntityLink id={plan.previous_candidate_id}>{tr("查看精确来源候选")}</EntityLink>}</article>}
        {plan.reference_inputs?.map((input, index) => {
          const id = String(input.media_id ?? '')
          const stateId = String(input.state_id ?? '')
          const assetId = String(input.asset_id ?? '')
          const revisionId = String(input.revision_id ?? '')
          const revision = project.references?.[revisionId]
          const view = viewLabel(String(input.view ?? revision?.view ?? ''))
          const name = String(input.asset_name ?? project.assets?.[assetId]?.name ?? tr("参考素材"))
          const label = [name, String(input.state_label ?? project.states?.[stateId]?.label ?? tr("剧情状态")), view].filter(Boolean).join(' · ')
          return <article key={`${id}:${index}`}><h4>Picture {index + 1 + Number(Boolean(boundary))} · {plan.status === 'legacy' ? tr("原任务精确输入") : input.state_role === 'target' ? tr("目标状态") : tr("起始状态")}</h4><MediaPreview media={project.media?.[id]} title={[name, view].filter(Boolean).join(' · ')} />
            <p><EntityLink id={revisionId}>{label}</EntityLink></p>{revision && <p className="studio-note">{referenceName(revision)}</p>}<p>{String(input.purpose ?? tr("原任务未记录用途"))}</p>{input.prepared_reference_id ? <p className="studio-note">{tr("实际使用裁剪或准备后的图片；原始参考板保留在修订详情。")}</p> : null}
          </article>
        })}
        {(['video_inputs', 'audio_inputs'] as const).flatMap((key) => (plan[key] ?? []).map((input, index) => <article key={`${key}:${index}`}><h4>{key === 'video_inputs' ? 'Video' : 'Audio'} {index + 1}</h4><MediaPreview media={project.media?.[String(input.media_id ?? '')]} title={key === 'video_inputs' ? tr("视频参考片段") : tr("音色与声音参考")} /><p>{String(input.purpose ?? '')}</p><p>{tr("来源区间：")}{String(input.start_seconds ?? tr("未记录"))}–{String(input.end_seconds ?? tr("未记录"))} {tr("秒")}</p>{input.source_candidate_id ? <EntityLink id={String(input.source_candidate_id)}>{tr("查看来源视频")}</EntityLink> : null}{input.voice_reference_id ? <p className="studio-note">{tr("已固定的角色音色版本")}</p> : null}</article>))}
      </div>
      {plan.conflicts?.map((conflict) => <div key={conflict.id} className={conflict.status === 'open' ? 'reference-plan-warning' : 'studio-note'}><strong>{conflict.status === 'open' ? tr("参考冲突待解决") : conflict.status === 'withdrawn' ? tr("已核对并撤回误判") : conflict.status === 'not_applicable' ? tr("历史冲突不涉及本版输入") : tr("参考冲突已处理")}</strong><p>{conflict.description}</p>{conflict.reason && <p>{conflict.reason}</p>}{conflict.correction_reason && <p>{tr("纠正依据：")}{conflict.correction_reason}</p>}{conflict.applicability_reason && <p>{conflict.applicability_reason}{tr("。原观察及证据保留。")}</p>}</div>)}
      {plan.conflict_check && <LazyDetails title={tr("素材一致性核对")}><Markdown>{plan.conflict_check}</Markdown></LazyDetails>}
      {plan.prompt && <LazyDetails title={tr("按此清单编写的六段式提示词")}><Markdown>{plan.prompt}</Markdown><p>{plan.prompt_reconciliation}</p></LazyDetails>}
      {plan.reason && <p className="studio-note">{tr("修改依据：")}{plan.reason}</p>}
      {(plan.changes?.length ?? 0) > 0 && <LazyDetails title={tr("这一版修改了什么")}>{plan.changes?.map((change, index) => {
        const field = String(change.field ?? '')
        const labels: Record<string, string> = { bindings: tr("图片参考"), video_references: tr("视频参考"), audio_references: tr("声音参考"), conflicts: tr("冲突处理"), prompt: tr("提示词草稿") }
        const references = (value: unknown) => Array.isArray(value) ? value.filter((v): v is Record<string, unknown> => Boolean(v) && typeof v === 'object').map((v, i) => {
          const id = String(v.revision_id ?? v.preparation_job_id ?? v.voice_reference_id ?? '')
          return id ? <span key={`${id}:${i}`}><EntityLink id={id}>{String(v.purpose ?? tr("查看精确来源"))}</EntityLink>{i < value.length - 1 ? '、' : ''}</span> : null
        }) : null
        return <article className="studio-reference-diff" key={index}><h4>{labels[field] ?? field}</h4>{field === 'prompt' ? <p>{tr("提示词草稿已修改，定稿前须重新核对素材分工和顺序。")}</p> : <><p>{tr("之前：")}{Array.isArray(change.before) ? tr("{{0}} 项", change.before.length) : tr("未记录")} {references(change.before)}</p><p>{tr("现在：")}{Array.isArray(change.after) ? tr("{{0}} 项", change.after.length) : tr("未记录")} {references(change.after)}</p></>}</article>
      })}{plan.parent_id && <EntityLink id={plan.parent_id}>{tr("打开前一版完整清单")}</EntityLink>}</LazyDetails>}
      <LazyDetails title={tr("参考清单版本历史")}><ReferencePlanHistory project={project} clipId={plan.clip_id} onSelect={onSelect} /></LazyDetails>
    </> : !detail.loading && <p className="studio-empty-line">{tr("这份参考清单暂不可用。")}</p>}
  </section></EntityLinkProvider>
}

function ReferencePlanHistory({ project, clipId, onSelect }: { project: StudioProject; clipId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const history = useEntityHistory(project, 'reference_plans', clipId)
  return <>{history.items?.map((item) => <button key={String(item.id)} className="studio-usage" onClick={() => onSelect({ kind: 'reference-plan', id: String(item.id) })}>{tr("第 {{0}} 版 ·", item.revision)} {planLabels[String(item.status) as keyof typeof planLabels] ?? tr("历史清单")}</button>)}{history.controls}</>
}
