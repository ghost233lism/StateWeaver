import { tr, useLocale } from '../../ui/locale'
import { Headphones } from 'lucide-react'
import type { Asset, Candidate, Clip, Reference, Selection, StudioProject } from './model'
import { MediaPreview } from './Media'
import { Markdown } from './Markdown'
import { EntityText } from './EntityLinks'
import { LazyDetails, useEntityDetail } from './data'
import './ContinuityDetails.css'

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const array = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(object) : []
const string = (value: unknown) => typeof value === 'string' ? value : ''
const time = (value: unknown) => typeof value === 'number' ? tr("{{0}} 秒", Number(value.toFixed(2))) : tr("时间未记录")
type Navigate = (selection: Selection) => void

export function VoiceReferences({ project, asset, onSelect, initialVoiceReferenceId }: { project: StudioProject; asset: Asset & { id: string }; onSelect: Navigate; initialVoiceReferenceId?: string }) {
  useLocale()

  const detail = useEntityDetail(project, `asset:${asset.id}`)
  const current = detail.project.assets?.[asset.id] ?? asset
  const voices = current.voice_references ?? []
  if (!voices.length && !current.voice_bootstrap_clip_id) return null
  const selected = voices.find((voice) => voice.id === current.selected_voice_id)
  const voiceBody = (voice: typeof voices[number]) => <>
    {voice.media_id ? <MediaPreview media={detail.project.media?.[voice.media_id]} title={tr("{{0}}音色参考", asset.name)} /> : <p className="studio-note">{voice.status === 'failed' ? tr("音色提取失败，等待总导演处理。") : tr("正在从已选视频提取音色。")}</p>}
    <p className="studio-note">{tr("来源区间")} {time(voice.start_seconds)} — {time(voice.end_seconds)}</p>
    <Markdown>{voice.reason}</Markdown>
    <button className="studio-text-button" onClick={() => onSelect({ kind: 'entity', id: voice.source_candidate_id })}>{tr("查看音色来源视频")}</button>
  </>
  return <section className="studio-voice-reference" aria-label={tr("{{0}}固定音色", asset.name)}>
    <h3><Headphones size={18} />{tr("固定音色")}</h3>{detail.status}
    {selected ? <><p className="studio-note">{tr("当前选用 · 版本")} {voices.indexOf(selected) + 1}</p>{voiceBody(selected)}</> : <p className="studio-note">{tr("等待正式发声片段选用后建立音色。")}</p>}
    {voices.some((v) => v.id !== selected?.id) && <div className="studio-voice-history"><h4>{tr("音色版本")}</h4>{voices.map((voice, index) => voice.id === selected?.id ? null : <details key={voice.id} open={voice.id === initialVoiceReferenceId || undefined}>
      <summary>{tr("版本")} {index + 1} · {voice.status === 'preparing' ? tr("正在提取") : voice.status === 'failed' ? tr("提取失败") : tr("历史参考")}</summary>{voiceBody(voice)}
    </details>)}</div>}
    <p className="studio-note">{tr("用于延续人物的声音特点；台词按当前剧本生成。")}</p>
  </section>
}

export function SelectionNotice({ value }: { value: Candidate | Reference }) {
  useLocale()

  if (value.selection?.kind !== 'director_compromise' && value.selection?.kind !== 'system_fallback') return null
  return <section className="studio-compromise-notice"><h3>{value.selection.kind === 'system_fallback' ? tr("系统兜底选用") : tr("总导演妥协选用")}</h3><Markdown>{value.selection.reason}</Markdown><h4>{tr("保留的偏差")}</h4><ul>{value.selection.deviations?.map((deviation, index) => <li key={index}><EntityText>{deviation}</EntityText></li>)}</ul><p className="studio-note">{tr("原始检查与剧本均保留，这次选用不代表检查通过。")}</p></section>
}

export function RepairDetails({ plan }: { plan: Candidate['repair_plan'] }) {
  useLocale()

  if (!plan) return null
  return <section className="studio-repair-plan"><h3>{tr("本次定向修正")}</h3><Markdown>{plan.change}</Markdown><dl><div><dt>{tr("需要修正")}</dt><dd>{plan.defects?.map((text, i) => <p key={i}><EntityText>{text}</EntityText></p>)}</dd></div><div><dt>{tr("保持不变")}</dt><dd>{plan.preserve?.map((text, i) => <p key={i}><EntityText>{text}</EntityText></p>)}</dd></div></dl></section>
}

export function QualityEpisodeDetail({ clip }: { clip: Clip }) {
  useLocale()

  const episode = clip.quality_episode
  if (!episode) return null
  return <LazyDetails title={tr("总导演质量处理记录")}><p className="studio-note">{tr("普通修正")} {clip.repair_count ?? 0} {tr("次；总导演追加已用")} {episode.extra_used ?? 0}{tr("／总上限")} {episode.extra_limit ?? 3} {tr("次。")}</p>{episode.decisions?.map((decision, index) => <article className="studio-quality-decision" key={index}><strong>{{ escalate: tr("提交总导演"), retry: tr("追加方案"), select: tr("选用候选"), compromise: tr("妥协选用") }[decision.action]}{decision.attempts ? tr(" · {{0}} 次", decision.attempts) : ''}</strong><Markdown>{decision.reason}</Markdown></article>)}</LazyDetails>
}

export function CandidateContinuity({ project, candidate, onSelect }: { project: StudioProject; candidate: Candidate; onSelect: Navigate }) {
  useLocale()

  const request = object(project.jobs?.[candidate.job_id]?.request)
  const timed = [...array(request.video_inputs).map((input) => ({ ...input, modality: tr("视频") })), ...array(request.audio_inputs).map((input) => ({ ...input, modality: tr("声音") }))] as Record<string, unknown>[]
  const plans = array(request.asset_plan)
  const observations = candidate.observed_asset_states ?? {}
  const roleNames: Record<string, string> = { continuation: tr("前段尾部接续"), edit_source: tr("本片段修正来源"), motion: tr("动作参考"), identity: tr("身份参考"), voice: tr("固定人物音色"), ambience: tr("环境声参考"), sound: tr("声音特点参考") }
  return <>
    <RepairDetails plan={candidate.repair_plan} />
    {(plans.length > 0 || Object.keys(observations).length > 0) && <section><h3>{tr("本片段资产状态")}</h3>{Array.from(new Set([...plans.map((p) => string(p.asset_id)), ...Object.keys(observations)])).map((assetId) => {
      const plan = plans.find((item) => item.asset_id === assetId)
      return <article className="studio-clip-state" key={assetId}><h4>{project.assets?.[assetId]?.name ?? tr("资产")}</h4>{plan && <p>{tr("计划：")}{project.states?.[string(plan.start_state_id)]?.label ?? tr("起始状态")}{plan.end_state_id && plan.end_state_id !== plan.start_state_id ? ` → ${project.states?.[string(plan.end_state_id)]?.label ?? tr("结束状态")}` : ''}</p>}<p>{tr("实际观察：")}<EntityText>{observations[assetId] ?? tr("尚未记录，不能根据计划推定实际结果。")}</EntityText></p><button className="studio-text-button" onClick={() => onSelect({ kind: 'asset', id: assetId, stateId: plan ? string(plan.start_state_id) : undefined })}>{tr("查看资产状态")}</button></article>
    })}</section>}
    {timed.length > 0 && <section className="studio-timed-references"><h3>{tr("本次视频与声音参考")}</h3>{timed.map((input, index) => <article key={`${input.modality}:${input.media_id}:${index}`}><h4>{roleNames[string(input.role)] ?? string(input.modality)}</h4><Markdown>{string(input.purpose)}</Markdown><p className="studio-note">{time(input.duration)} {tr("· 来源")} {time(input.start_seconds)} — {time(input.end_seconds)}</p><MediaPreview media={project.media?.[string(input.media_id)]} title={roleNames[string(input.role)] ?? tr("参考素材")} />{Boolean(input.source_candidate_id) && <button className="studio-text-button" onClick={() => onSelect({ kind: 'entity', id: string(input.source_candidate_id) })}>{tr("查看来源视频")}</button>}</article>)}</section>}
  </>
}
