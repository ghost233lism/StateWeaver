import { getLocale, tr, trSystem, useLocale } from '../../ui/locale'
import { ChevronDown, ChevronUp, Clock3 } from 'lucide-react'
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import type { StudioClient } from './client'
import { roleLabels, type ProductionETA, type ProductionStatus, type Selection, type StudioProject } from './model'
import { PlaybackDetails, PlaybackSummary } from './PlaybackDuration'
import { SourceTraceability } from './SourceTraceability'
import { LifecycleDiagnostics } from './LifecycleDiagnostics'
import './ProductionProgress.css'

const ProductionContext = createContext<ProductionStatus | null>(null)
export const useProductionStatus = () => useContext(ProductionContext)

export function productionDuration(seconds: number) {
  if (seconds < 60) return tr("不足 1 分钟")
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return tr("{{0}} 分钟", minutes)
  return tr("{{0}} 小时{{1}}", Math.floor(minutes / 60), minutes % 60 ? tr(" {{0}} 分钟", minutes % 60) : '')
}

export function etaLabel(eta: ProductionETA) {
  if (eta.status === 'complete') return tr("成片已完成")
  if (eta.minimum_seconds == null || eta.maximum_seconds == null) return tr("暂无法估算剩余时间")
  const minimum = productionDuration(eta.minimum_seconds), maximum = productionDuration(eta.maximum_seconds)
  return `${eta.status === 'conditional' ? tr("解除阻塞后预计还需") : tr("预计还需")} ${minimum === maximum ? minimum : `${minimum}–${maximum}`}`
}

const materialLabels: Record<string, string> = { get ready() { return tr("已就绪") }, get preparing() { return tr("准备中") }, get review() { return tr("等待检查选用") }, get waiting_source() { return tr("等待来源实拍") }, get needed() { return tr("待准备") }, get not_required() { return tr("当前无需准备") }, get blocked() { return tr("结果待核实") } }
const phaseLabels: Record<string, string> = { get director() { return tr("导演运行（含工具）") }, get review() { return tr("候选自审（含工具）") }, get mcp() { return tr("MCP 工具") }, get image() { return tr("图片生成") }, get reference() { return tr("本地参考处理") }, get evidence() { return tr("审核证据准备") }, get assembly() { return tr("成片合成") }, get video_io() { return tr("视频提交、查询与回收") }, get http() { return tr("页面 API 处理") } }
const owners: Record<string, string> = { get user() { return tr("用户") }, get technical() { return tr("技术处理") } }

export function ProductionScope({ client, project, onSelect, children }: {
  client: StudioClient; project: StudioProject; onSelect: (selection: Selection) => void; children: ReactNode
}) {
  useLocale()

  const [status, setStatus] = useState<ProductionStatus | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [unavailable, setUnavailable] = useState(false)
  const refresh = useRef<() => void>(() => {})
  useEffect(() => {
    if (!client.productionStatus || !project.id) return
    const projectId = project.id
    let disposed = false, pending = false
    const abort = new AbortController()
    const load = () => {
      if (pending || document.visibilityState === 'hidden') return
      pending = true
      void client.productionStatus!(projectId, expanded, abort.signal).then((value) => {
        if (!disposed && value.project_id === projectId) { setStatus(value); setUnavailable(false) }
      }).catch(() => { if (!disposed) setUnavailable(true) }).finally(() => { pending = false })
    }
    refresh.current = load
    const initial = window.setTimeout(load, 150)
    const interval = window.setInterval(load, 15000)
    document.addEventListener('visibilitychange', load)
    return () => { disposed = true; abort.abort(); clearTimeout(initial); clearInterval(interval); document.removeEventListener('visibilitychange', load) }
  }, [client, project.id, expanded])
  useEffect(() => {
    const timer = window.setTimeout(() => refresh.current(), 1500)
    return () => clearTimeout(timer)
  }, [project.revision, project.status])
  const current = status?.project_id === project.id ? status : null
  return <ProductionContext.Provider value={current}>
    {client.productionStatus && <section className="studio-production-progress" aria-label={tr("项目制作进展")}>
      <button className="studio-production-summary" onClick={() => setExpanded(!expanded)} aria-expanded={expanded} aria-controls="studio-production-detail">
        <span className="studio-production-count">{current ? tr("{{0}} / {{1}} 条 Clip 已选用", current.selected_clips, current.planned_clips) : tr("读取制作进展…")}</span>
        {current?.playback && <PlaybackSummary playback={current.playback} />}
        {current && <span className="studio-production-eta"><Clock3 size={15} />{etaLabel(current.eta)}{current.eta.confidence === 'low' && <small>{tr("粗估")}</small>}</span>}
        <span className="studio-production-blocker">{unavailable ? tr("进展暂时无法更新") : trSystem(current?.blockers?.[0]?.description ?? '') || (current?.status === 'complete' ? tr("成片可播放") : '')}</span>
        <span className="studio-production-disclosure">{tr("详情")}{expanded ? <ChevronUp size={17} /> : <ChevronDown size={17} />}</span>
      </button>
      {expanded && <div className="studio-production-detail" id="studio-production-detail">
        {!current ? <p>{tr("正在读取进展与估算依据。")}</p> : <>
          <div className="studio-production-detail-grid">
            <div><h3>{tr("预计时间与规划覆盖")}</h3><p>{etaLabel(current.eta)}</p><p>{current.planned_sets} / {current.total_sets} {tr("个片场已拆分 Clip；已历时")} {productionDuration(current.elapsed_seconds)}。</p>
              {current.eta.uncovered?.map((note) => <p className="studio-production-note" key={note}>{trSystem(note)}</p>)}
              <details><summary>{tr("估算依据")}</summary><ul>{current.eta.assumptions?.map((note) => <li key={note}>{trSystem(note)}</li>)}</ul><p>{tr("匹配的视频速度样本：")}{current.eta.sample_counts?.video ?? 0}。</p></details>
            </div>
            <div><h3>{tr("当前待处理")}</h3>{current.blockers?.length ? <ul className="studio-production-blockers">{current.blockers.map((blocker) => <li key={blocker.key}><span>{trSystem(blocker.description)}</span>{blocker.owner_id ? <button className="studio-text-button" onClick={() => onSelect({ kind: 'actor', id: blocker.owner_id! })}>{owners[blocker.owner_role] ?? roleLabels[blocker.owner_role as keyof typeof roleLabels] ?? blocker.owner_role}{blocker.set_id && project.sets?.[blocker.set_id] ? ` · ${project.sets[blocker.set_id].title}` : ''}</button> : <small>{owners[blocker.owner_role] ?? roleLabels[blocker.owner_role as keyof typeof roleLabels] ?? blocker.owner_role}</small>}</li>)}</ul> : <p>{tr("没有当前阻塞。")}</p>}</div>
          </div>
          {current.playback && <PlaybackDetails playback={current.playback} />}
          <LifecycleDiagnostics value={current.lifecycle} project={project} onSelect={onSelect} />
          <SourceTraceability value={current.lifecycle?.source_traceability} project={project} onSelect={onSelect} />
          <details className="studio-production-materials"><summary>{tr("素材准备")}{Object.entries(current.material_counts ?? {}).map(([kind, count]) => ` · ${materialLabels[kind] ?? kind} ${count}`).join('')}</summary>
            {!current.materials?.length ? <p>{tr("当前没有待展示的素材状态。")}</p> : <ul>{current.materials.map((material) => <li key={material.state_id}><button className="studio-text-button" onClick={() => onSelect({ kind: 'asset', id: material.asset_id, stateId: material.state_id })}>{project.assets?.[material.asset_id]?.name ? `${project.assets[material.asset_id].name} · ` : ''}{material.label}</button><span>{materialLabels[material.status]}</span>{!!material.missing_views?.length && <small>{tr("待补：")}{material.missing_views.join('、')}</small>}</li>)}</ul>}
          </details>
          <details><summary>{tr("分项耗时")}</summary>{current.timings?.length ? <div className="studio-production-table"><table><thead><tr><th>{tr("环节")}</th><th>{tr("累计任务时间")}</th><th>{tr("次数")}</th><th>{tr("单次中位数")}</th><th>p95</th></tr></thead><tbody>{current.timings.map((timing) => <tr key={timing.phase}><th>{phaseLabels[timing.phase] ?? timing.phase}</th><td>{productionDuration(timing.total_seconds)}</td><td>{timing.count}</td><td>{timing.p50_seconds == null ? '—' : tr("{{0}} 秒", timing.p50_seconds.toFixed(2))}</td><td>{timing.p95_seconds == null ? tr("样本不足") : tr("{{0}} 秒", timing.p95_seconds.toFixed(2))}</td></tr>)}</tbody></table></div> : <p>{tr("本版尚未积累计时记录，历史耗时不补造。")}</p>}{current.notes?.map((note) => <p className="studio-production-note" key={note}>{trSystem(note)}</p>)}</details>
          {current.as_of && <small className="studio-production-updated">{tr("更新于")} {new Date(current.as_of).toLocaleTimeString(getLocale())}</small>}
        </>}
      </div>}
    </section>}
    {children}
  </ProductionContext.Provider>
}
