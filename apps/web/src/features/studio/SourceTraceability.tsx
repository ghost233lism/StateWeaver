import { tr, useLocale } from '../../ui/locale'
import type { Selection, StudioProject } from './model'
import { record, records, textValue } from './candidate-details'
import { LazyDetails } from './data'
import { ReadingText } from './ReadingText'

const diagnosisLabels: Record<string, string> = {
  get mapping_missing() { return tr("原镜头尚未绑定 Clip") }, get mapped_clip_unavailable() { return tr("映射中的 Clip 已不可用") },
  get unmapped_clip() { return tr("Clip 尚未对应原镜头") }, get noncontiguous_clip_mapping() { return tr("对应的 Clip 不连续") },
  get source_order_mismatch() { return tr("输出顺序与原镜头不一致") }, get selection_missing() { return tr("Clip 尚未选用候选") },
  get actual_duration_unknown() { return tr("实际片长尚未确认") }, get duplicate_video_coverage() { return tr("多个原镜头重复使用同一 Clip") },
  get mapping_changed_since_generation() { return tr("映射已调整，生成时的原要求仍保留") },
  get assembly_duration_differs_from_segments() { return tr("成片时长与源片段合计不一致") },
}
const time = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? tr("{{0}} 秒", Number(value.toFixed(2))) : tr("待确认")

export function SourceTraceability({ value, project, onSelect }: {
  value: unknown; project: StudioProject; onSelect: (selection: Selection) => void
}) {
  useLocale()

  const trace = record(value), outputs = records(trace.outputs), diagnostics = records(trace.diagnostics)
  if (!outputs.length) return null
  return <LazyDetails title={tr("原镜头与交付片段对应")}>
    <p>{outputs.length} {tr("个原始输出，对应")} {typeof trace.clip_count === 'number' ? trace.clip_count : tr("待确认数量的")} {tr("条 Clip。")}</p>
    <p className="studio-production-note">{tr("成片位置按已选视频的实际片长计算。缺少片长时显示待确认。")}</p>
    {diagnostics.length > 0 && <LazyDetails title={tr("待核对的对应关系（{{0}} 项）", diagnostics.length)}><ul>{diagnostics.map((item, index) => <li key={index}>{textValue(item.output_id) || project.clips?.[textValue(item.clip_id)]?.title || textValue(item.clip_id)}：{diagnosisLabels[textValue(item.reason)] ?? textValue(item.reason)}</li>)}</ul></LazyDetails>}
    {outputs.map((output) => <LazyDetails key={textValue(output.output_id)} title={tr("{{0}} · {{1}} 条 Clip · {{2}} — {{3}}", textValue(output.output_id), records(output.segments).length, time(output.film_start_seconds), time(output.film_end_seconds))}>
      <h4>{tr("原始要求")}</h4><p className="studio-source-original" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{textValue(output.source_prompt)}</p>
      {typeof output.frame_seconds === 'number' && <p>{tr("抽帧位置：对应片段内")} {time(output.frame_seconds)}。</p>}
      {Boolean(output.mapping_reason) && <ReadingText text={textValue(output.mapping_reason)} limit={240} label={tr("拆分说明")} />}
      {records(output.segments).length ? <div className="studio-production-table"><table><thead><tr><th scope="col">{tr("片场 / Clip")}</th><th scope="col">{tr("成片位置")}</th><th scope="col">{tr("选用候选")}</th></tr></thead><tbody>{records(output.segments).map((segment) => {
        const clipId = textValue(segment.clip_id), candidateId = textValue(segment.candidate_id)
        const kind = textValue(record(segment.selection).kind)
        const selection = kind === 'system_fallback' ? tr("系统兜底选用") : kind === 'director_compromise' ? tr("总导演妥协选用") : kind === 'user_selection' ? tr("用户临时选用") : kind === 'quality_passed' ? tr("检查通过并选用") : tr("查看已选候选")
        return <tr key={clipId}><td>{project.sets?.[textValue(segment.set_id)]?.title ?? textValue(segment.set_id)}<br /><button className="studio-text-button" onClick={() => onSelect({ kind: 'clip', id: clipId })}>{project.clips?.[clipId]?.title ?? clipId}</button></td><td>{time(segment.film_start_seconds)} — {time(segment.film_end_seconds)}</td><td>{candidateId ? <button className="studio-text-button" onClick={() => onSelect({ kind: 'entity', id: candidateId })}>{selection}</button> : tr("尚未选用")}</td></tr>
      })}</tbody></table></div> : <p>{tr("尚未绑定可用 Clip。")}</p>}
    </LazyDetails>)}
  </LazyDetails>
}
