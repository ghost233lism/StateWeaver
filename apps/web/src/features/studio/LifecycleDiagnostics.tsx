import { tr, trSystem, useLocale } from '../../ui/locale'
import type { Selection, StudioProject } from './model'
import { label, roleLabels } from './model'
import { record, recordedTime, records, textValue } from './candidate-details'

const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? String(value) : tr("未记录")
const candidateCounts = [
  ['generating_candidates', '生成中候选'], ['generated_candidates', '已返回视频候选'],
  ['reviewed_candidates', '已审核候选'], ['pending_review_candidates', '待审核候选'],
  ['pending_selection_assessments', '待比较候选'], ['technical_dispositions', '技术终止候选'],
] as const
const selectionCounts = [
  ['quality_passed_selections', '检查通过选用'], ['director_compromise_selections', '总导演妥协选用'],
  ['system_fallback_selections', '系统兜底选用'], ['user_selections', '用户选用'],
  ['selected_without_kind', '未记录选用类型'], ['failed_clips', '失败 Clip'],
] as const
const waitLabels: Record<string, string> = { get provider() { return tr("等待服务返回") }, get dependency() { return tr("等待依赖") }, get review() { return tr("等待审核") } }
const wakeLabels: Record<string, string> = { get target_state_changed() { return tr("等待对象已变化，待调度核对") }, get actor_control_changed() { return tr("导演控制状态已变化，待调度核对") }, get unregistered() { return tr("未登记有效等待对象") } }
const targetLabels: Record<string, string> = { get job() { return tr("服务任务") }, get candidate() { return tr("候选") }, clip: 'Clip', get set() { return tr("片场") }, get reference() { return tr("参考素材") }, get delegate_task() { return tr("委派任务") }, get visual_task() { return tr("视觉任务") }, get proposal() { return tr("提案") } }

export function LifecycleDiagnostics({ value, project, onSelect }: {
  value: unknown; project: StudioProject; onSelect: (selection: Selection) => void
}) {
  useLocale()

  const lifecycle = record(value), counts = record(lifecycle.counts), recovery = record(lifecycle.technical_recovery)
  if (lifecycle.policy !== 'coherent-v1') return null
  const waits = records(lifecycle.waits), progressAt = recordedTime(lifecycle.last_meaningful_at)
  return <section className="studio-lifecycle-detail" aria-label={tr("生成与审核状态")}>
    <h3>{tr("生成与审核状态")}</h3>
    {lifecycle.delivery_state === 'inconsistent_completion' && <p>{tr("完成状态与交付记录不一致，仍需核对。")}</p>}
    {lifecycle.delivery_state === 'failed' && <p>{tr("制作已终止，未能完成整片交付；已生成素材保留。")}</p>}
    <div className="studio-production-detail-grid">
      <div><h4>{tr("候选处理（个）")}</h4><dl className="studio-lifecycle-counts">{candidateCounts.map(([key, title]) => <div key={key}><dt>{tr(title)}</dt><dd>{count(counts[key])}</dd></div>)}</dl></div>
      <div><h4>{tr("Clip 选用与结果（条）")}</h4><dl className="studio-lifecycle-counts">{selectionCounts.map(([key, title]) => <div key={key}><dt>{tr(title)}</dt><dd>{count(counts[key])}</dd></div>)}</dl></div>
    </div>
    <p className="studio-production-note">{tr("已审核包括通过和未通过；妥协选用与系统兜底保留原审核结论。待比较表示仍缺少选用评估。")}</p>
    <p>{tr("进行中任务：")}{count(counts.active_job_obligations)} {tr("个 · 技术恢复终止：")}{count(recovery.terminal_recoveries)} {tr("个任务")}</p>
    <p>{tr("技术重试：")}{count(recovery.retry_records)} {tr("次 · 分析重试：")}{count(recovery.analysis_retry_records)} {tr("次")}{recovery.polls_are_retries === false && tr("（正常轮询不计入重试）")}</p>
    <p>{tr("最后有效进展：")}{progressAt ? <time dateTime={textValue(lifecycle.last_meaningful_at)}>{progressAt}</time> : tr("尚无有效进展记录")}</p>
    <details><summary>{tr("等待对象 ·")} {count(counts.waiting_actors)} {tr("位导演处于等待状态")}</summary>
      {lifecycle.waits_truncated === true && <p className="studio-production-note">{tr("仅展示前")} {waits.length} {tr("位，共")} {count(counts.waiting_actors)} {tr("位；计数包含未展示项。")}</p>}
      {!waits.length ? <p>{tr("本次响应没有等待明细。")}</p> : <ul className="studio-production-blockers">{waits.map((wait, index) => {
        const actorId = textValue(wait.actor_id), actor = project.actors?.[actorId], setId = textValue(wait.set_id)
        const actorLabel = actor ? roleLabels[actor.role] ?? actor.role : tr("查看导演")
        const targets = records(wait.targets)
        return <li key={actorId || index}>
          {actorId && <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: actorId })}>{actorLabel}{setId && project.sets?.[setId] ? ` · ${project.sets[setId].title}` : ''}</button>}
          <span>{wait.waiting === true ? waitLabels[textValue(wait.kind)] ?? tr("等待中") : wakeLabels[textValue(wait.waiting_reason)] ?? tr("等待条件已变化，待调度核对")}</span>
          {Boolean(wait.reason) && <span>{trSystem(textValue(wait.reason))}</span>}
          {targets.length > 0 && <ul>{targets.map((target, targetIndex) => {
            const id = textValue(target.id), kind = textValue(target.kind)
            const name = kind === 'clip' ? project.clips?.[id]?.title : kind === 'set' ? project.sets?.[id]?.title : undefined
            return <li key={`${kind}:${id}:${targetIndex}`}>{targetLabels[kind] ?? kind} · {name || id}{Boolean(target.status) && ` · ${label(textValue(target.status))}`}</li>
          })}</ul>}
          {wait.targets_truncated === true && <small>{tr("仅展示")} {targets.length} {tr("个对象，共")} {count(wait.target_count)} {tr("个。")}</small>}
        </li>
      })}</ul>}
    </details>
  </section>
}
