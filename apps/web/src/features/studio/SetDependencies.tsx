import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useState } from 'react'
import { LazyDetails, useStudioData } from './data'
import { EntityLink } from './EntityLinks'
import { generationDependencies, type Selection, type SetDependencyReport, type StudioProject } from './model'
import './SetDependencies.css'

const modes: Record<string, string> = { get independent() { return tr("独立准备") }, get shared() { return tr("共享固定设定") }, get observed() { return tr("继承实拍") }, get whole_set() { return tr("等待片场完成") } }
const statuses: Record<string, string> = { get independent() { return tr("可提前准备") }, get shared() { return tr("可提前准备") }, get ready() { return tr("承接已确认") }, get waiting_source() { return tr("等待实拍选片") }, get needs_confirmation() { return tr("等待总导演核对") }, get stale() { return tr("需要重新核对") }, get invalid() { return tr("需要修正约定") } }
const text = (value: unknown) => typeof value === 'string' ? value : ''
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

export function SetDependencies({ project, setId, onSelect }: { project: StudioProject; setId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const set = project.sets?.[setId]
  if (!set) return null
  return <div className="studio-set-dependencies"><p className="studio-note">{tr("成片顺序")} {String(set.order + 1).padStart(2, '0')} · {generationDependencies(set).length} {tr("个生成依赖")}</p>
    <LazyDetails title={tr("资产承接与等待原因")}><DependencyDetails key={`${project.id}:${setId}`} project={project} setId={setId} onSelect={onSelect} /></LazyDetails>
  </div>
}

function DependencyDetails({ project, setId, onSelect }: { project: StudioProject; setId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const source = useStudioData()
  const [report, setReport] = useState<SetDependencyReport>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const set = project.sets?.[setId]
  const revision = source?.invalidations?.[`set:${setId}`]
  const memoryRevision = source?.invalidations?.['memory:global']
  const sourceVersion = (set?.asset_handoffs ?? []).map((item) => item.source_set_id ?
    (project.sets?.[item.source_set_id]?.clip_ids ?? []).map((id) => `${id}:${project.clips?.[id]?.accepted_candidate_id ?? ''}`).join(',') : '').join('|')
  useEffect(() => {
    if (!source?.client.setDependencies) return
    const controller = new AbortController()
    setError('')
    void source.client.setDependencies(source.projectId, setId, controller.signal).then((value) => {
      if (!controller.signal.aborted) setReport(value)
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "承接信息加载失败") })
    return () => controller.abort()
  }, [source?.client, project.id, setId, revision, set?.dependency_revision, sourceVersion, memoryRevision, retry])
  if (error) return <p role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((n) => n + 1)}>{tr("重试")}</button></p>
  if (!report) return <p role="status">{source?.client.setDependencies ? tr("正在核对承接条件…") : tr("此连接暂不提供承接详情。")}</p>
  const executive = Object.values(project.actors ?? {}).find((actor) => actor.role === 'executive')
  const priority = (item: Record<string, unknown>) => ['independent', 'shared'].includes(text(item.status)) ? 2 : item.status === 'ready' ? 1 : 0
  const items = [...(report.items ?? [])].sort((a, b) => priority(a) - priority(b))
  return <div className="studio-handoff-details">
    <p className="studio-handoff-stages"><span>{report.can_plan ? tr("可以继续规划") : tr("等待剧情交付")}</span><strong>{report.blockers?.length ? tr("部分承接尚未就绪") : tr("承接条件已就绪")}</strong></p>
    <p className="studio-note">{tr("每条 Clip 仅等待自身所需状态；实际生成还会检查素材与前段接续。")}</p>
    {items.map((item, index) => {
      const id = text(item.id) || String(index), assetId = text(item.asset_id), sourceSetId = text(item.source_set_id)
      const sourceSet = project.sets?.[sourceSetId]
      return <article key={id} className="studio-handoff-item">
        <div className="studio-handoff-heading"><strong>{assetId ? <EntityLink id={assetId}>{text(item.asset_name) || undefined}</EntityLink> : modes[text(item.mode)]}</strong><span data-status={text(item.status)}>{statuses[text(item.status)] ?? text(item.status)}</span></div>
        {assetId && <small>{modes[text(item.mode)] ?? text(item.mode)}</small>}
        {sourceSetId && <p>{tr("来源：")}{sourceSet ? <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: sourceSet.actor_id })}>{sourceSet.title}</button> : sourceSetId}{text(item.source_event_id) && <> · <EntityLink id={text(item.source_event_id)}>{text(item.source_event_title) || text(item.source_event_id)}</EntityLink></>}</p>}
        {strings(item.state_ids).length > 0 && <div className="studio-handoff-links">{strings(item.state_ids).map((stateId) => <EntityLink key={stateId} id={stateId} />)}</div>}
        {strings(item.required_attributes).length > 0 && <p>{tr("继承：")}{strings(item.required_attributes).join('、')}</p>}
        {text(item.reason) && <p>{text(item.reason)}</p>}
        {text(item.next_action) && <p className="studio-note">{text(item.next_action)}</p>}
        {strings(item.observation_ids).length > 0 && <div className="studio-handoff-links">{strings(item.observation_ids).map((entryId, i) => <EntityLink key={entryId} id={entryId}>{tr("实拍依据")} {i + 1}</EntityLink>)}</div>}
      </article>
    })}
    {(report.blockers ?? []).length > 0 && <div className="studio-handoff-blockers"><h4>{tr("待处理")}</h4><ul>{report.blockers?.map((item) => <li key={item.key}>{trSystem(item.description)}</li>)}</ul>{executive?.id && <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: executive.id! })}>{tr("由总导演协调")}</button>}</div>}
    {(report.preserved_clip_ids?.length ?? 0) > 0 && <p className="studio-note">{tr("已保留")} {report.preserved_clip_ids!.length} {tr("条选用片段。")}</p>}
  </div>
}
