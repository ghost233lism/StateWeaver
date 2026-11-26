import { tr, useLocale } from '../../ui/locale'
import { Eye, MessageSquare } from 'lucide-react'
import { Status } from './Status'
import { EntityLink, EntityText } from './EntityLinks'
import { LazyDetails, useEntityDetail, useEntityHistory } from './data'
import { ReadingText } from './ReadingText'
import { entries, type Selection, type StudioProject } from './model'
import './VisualAnalysis.css'

const categories: Record<string, string> = { get identity() { return tr("身份") }, get state() { return tr("状态") }, get action() { return tr("动作") }, get space() { return tr("空间") }, get camera() { return tr("运镜") }, get other() { return tr("其他") } }

export function VisualTaskList({ project, actorId, onSelect }: { project: StudioProject; actorId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const history = useEntityHistory(project, 'visual_tasks', actorId)
  const tasks = history.ids ? history.ids.flatMap((id) => history.project.visual_tasks?.[id] ? [history.project.visual_tasks[id]] : [])
    : entries(project.visual_tasks).filter((task) => task.parent_actor_id === actorId).sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
  return <div className="studio-visual-tasks" aria-label={tr("委托看图任务")}>
    {tasks.length ? tasks.map((task) => <button key={task.id} className="studio-visual-task-link" onClick={() => onSelect({ kind: 'visual-task', id: task.id! })}><span><Eye size={15} aria-hidden="true" />{task.question}</span><Status value={task.status ?? 'queued'} />{task.conclusion && <span className="studio-visual-task-preview">{task.conclusion}</span>}</button>) : <p className="studio-note">{tr("暂无委托看图任务。")}</p>}
    {history.controls}
  </div>
}

export function VisualTaskPanel({ project, taskId, onSelect }: { project: StudioProject; taskId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const detail = useEntityDetail(project, `visual-task:${taskId}`)
  const task = detail.project.visual_tasks?.[taskId]
  return <div className="studio-visual-task-panel">{detail.status}{task ? <>
    <header><Status value={task.status ?? 'queued'} /><button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: task.actor_id })}><MessageSquare size={15} />{tr("查看分析对话")}</button></header>
    <h3>{tr("核查任务")}</h3><ReadingText text={task.question} label={tr("核查要求")} />
    {(task.candidate_ids?.length ?? 0) > 0 && <div className="studio-visual-task-targets">{task.candidate_ids?.map((id) => <EntityLink key={id} id={id}>{tr("查看候选")}</EntityLink>)}</div>}
    {(task.revision_ids?.length ?? 0) > 0 && <div className="studio-visual-task-targets">{task.revision_ids?.map((id) => <EntityLink key={id} id={id}>{tr("查看素材版本")}</EntityLink>)}</div>}
    {task.conclusion && <section><h3>{tr("分析结论")}</h3><ReadingText text={task.conclusion} label={tr("分析结论")} /></section>}
    {(task.findings?.length ?? 0) > 0 && <section><h3>{tr("画面问题")}</h3>{task.findings?.map((finding, index) => <article key={index} className="studio-visual-finding"><p className="studio-note">{finding.start_seconds.toFixed(2)}{finding.end_seconds != null ? `–${finding.end_seconds.toFixed(2)}` : ''} {tr("秒 ·")} {categories[finding.category ?? 'other']} · {finding.severity === 'minor' ? tr("轻微问题") : tr("需要修复")}</p><ReadingText text={finding.description} label={tr("问题说明")} /><div>{finding.evidence_media_ids.map((id, i) => <EntityLink key={id} id={id}>{tr("画面依据")} {i + 1}</EntityLink>)}</div></article>)}</section>}
    {(task.limitations?.length ?? 0) > 0 && <section className="studio-visual-limitations"><h3>{tr("未确认范围")}</h3><ul>{task.limitations?.map((item, index) => <li key={index}><EntityText>{item}</EntityText></li>)}</ul></section>}
    <LazyDetails title={tr("实际查看的证据 · {{0}} 张", task.viewed_media_ids?.length ?? 0)}><div className="studio-visual-evidence">{task.viewed_media_ids?.map((id, index) => <EntityLink key={id} id={id}>{tr("证据")} {index + 1}</EntityLink>)}</div></LazyDetails>
    <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: task.parent_actor_id })}>{tr("返回总导演决策记录")}</button>
  </> : detail.loaded && <p className="studio-note">{tr("未找到这项看图任务。")}</p>}</div>
}
