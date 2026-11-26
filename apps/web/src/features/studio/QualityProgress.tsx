import { tr, useLocale } from '../../ui/locale'
import { EntityLink, EntityText } from './EntityLinks'
import { roleLabels, type QualityProgress, type Selection, type StudioProject } from './model'
import './QualityProgress.css'

const operations: Record<string, string> = {
  get generate_clip() { return tr("提交生成") }, get repair_clip() { return tr("登记或调整修复") }, get review_candidate() { return tr("审核已有候选") },
  get resolve_quality() { return tr("总导演决定选用或追加") }, get prepare_references() { return tr("准备参考与提示词") },
  get supply_repair_references() { return tr("补充本次修复素材") }, get continue_next_clip() { return tr("继续下一段") },
}

export function QualityProgressPanel({ project, progress, onSelect }: {
  project: StudioProject; progress: QualityProgress; onSelect: (value: Selection) => void
}) {
  useLocale()

  const owner = progress.responsible_actor_id && project.actors?.[progress.responsible_actor_id]
  return <section className="studio-quality-progress" aria-label={tr("生成与修复进度")}>
    <header><h3>{tr("生成与修复")}</h3>{progress.responsible_actor_id && <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: progress.responsible_actor_id! })}>{tr("负责人：")}{owner ? roleLabels[owner.role] : tr("导演")}</button>}</header>
    <table><caption>{tr("已完成次数")}</caption><thead><tr><th scope="col">{tr("阶段")}</th><th scope="col">{tr("生成")}</th><th scope="col">{tr("审核")}</th></tr></thead><tbody>
      {([[tr("首版"), progress.initial], [tr("普通修复"), progress.ordinary], [tr("追加修复"), progress.executive]] as const).map(([name, count]) => <tr key={name}><th scope="row">{name}</th><td>{count?.generations ?? 0}</td><td>{count?.reviews ?? 0}</td></tr>)}
    </tbody></table>
    <p className="studio-note">{tr("普通修复已登记")} {progress.ordinary_registered} / {progress.ordinary_limit} {tr("次，还可登记")} {progress.ordinary_remaining} {tr("次。")}</p>
    <p className="studio-note">{tr("追加额度：已批准")} {progress.extra_approved} {tr("次，已用")} {progress.extra_used} {tr("次，已批准待用")} {progress.extra_approved_unused} {tr("次；")}{progress.extra_grantable === null ? tr("可继续追加，次数不限。") : <>{tr("还可追加")} {progress.extra_grantable} {tr("次。")}</>}</p>
    {(progress.pending_repairs?.length ?? 0) > 0 && <div className="studio-quality-pending"><h4>{tr("已登记，尚未提交")}</h4>{progress.pending_repairs?.map((attempt) => <div key={attempt.id}><strong>{attempt.kind === 'ordinary' ? tr("普通") : tr("追加")}{tr("修复")} {attempt.ordinal} · {attempt.plan.mode === 'edit' ? tr("编辑完整候选") : tr("重新生成")}</strong><EntityText>{attempt.plan.change}</EntityText>{attempt.plan.source_candidate_id && <EntityLink id={attempt.plan.source_candidate_id}>{tr("查看修复依据")}</EntityLink>}</div>)}</div>}
    {(progress.allowed_operations?.length ?? 0) > 0 && <p className="studio-quality-actions"><span>{tr("当前可做")}</span>{progress.allowed_operations?.map((operation) => <span key={operation}>{operations[operation] ?? operation}</span>)}</p>}
    {(progress.blockers?.length ?? 0) > 0 && <ul className="studio-quality-blockers">{progress.blockers?.map((blocker, index) => <li key={`${blocker.code}:${index}`}><EntityText>{blocker.reason}</EntityText>{blocker.owner_actor_id && <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: blocker.owner_actor_id! })}>{project.actors?.[blocker.owner_actor_id] ? roleLabels[project.actors[blocker.owner_actor_id].role] : tr("负责人")}{tr("处理")}</button>}</li>)}</ul>}
  </section>
}
