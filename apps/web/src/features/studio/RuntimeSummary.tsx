import { getLocale, tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useState } from 'react'
import { entries, type Actor, type StudioProject } from './model'
import { EntityText } from './EntityLinks'
import { useProductionStatus } from './ProductionProgress'

const activeStatuses = new Set(['queued', 'submitting', 'running', 'ambiguous'])
const jobLabels: Record<string, string> = { get video() { return tr("生成视频") }, get image() { return tr("生成参考图") }, get reference() { return tr("准备参考素材") }, get analysis() { return tr("检查画面") }, get assembly() { return tr("合成短片") } }
export function taskSummary(project: StudioProject, actor: Actor) {
  const recovery = actor.image_input_recoveries?.at(-1)
  if (recovery && (recovery.status === 'retrying' || recovery.status === 'manual')) {
    return {
      label: recovery.status === 'manual' ? tr("图片输入受限，需人工处理") : tr("已隔离疑似图片，恢复中（1/1）"),
      target: recovery.status === 'manual' ? tr("自动恢复已停止，原视频与未审范围保留") : tr("排除 {{0}} 张疑似图片，继续原制作进度", recovery.media_ids?.length ?? 0),
      since: recovery.created_at, job: undefined, retryAt: null, lastProgress: actor.last_progress_at,
    }
  }
  const all = entries(project.jobs).filter((job) => activeStatuses.has(job.status))
  const own = all.filter((job) => job.actor_id === actor.id)
  const jobs = own.length ? own : actor.role === 'executive' && actor.status === 'waiting' ? all : []
  const job = jobs.sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))[0]
  const set = actor.set_id ? project.sets?.[actor.set_id] : undefined
  const dependencies = (set?.depends_on ?? []).map((id) => project.sets?.[id]).filter((s) => s && s.status !== 'complete')
  const nextClip = set?.clip_ids?.map((id) => project.clips?.[id]).find((item) => item && !item.accepted_candidate_id)
  const voices = (nextClip?.speaking_asset_ids ?? []).map((id) => project.assets?.[id]).filter((asset) => asset && !asset.selected_voice_id && asset.voice_bootstrap_clip_id !== nextClip?.id)
  const qualityWait = nextClip?.status === 'escalated'
  const candidate = job ? project.candidates?.[job.target_id] : undefined
  const clip = candidate ? project.clips?.[candidate.clip_id] : undefined
  const deferredCheck = job?.kind === 'analysis' && job.status === 'queued' && Boolean(job.error) && job.next_poll_at > 0
  const label = job?.query_outage_since ? tr("任务查询异常{{0}}", clip ? `：${clip.title}` : '') : deferredCheck ? tr("检查暂缓，自动恢复{{0}}", clip ? `：${clip.title}` : '') : job ? `${own.length ? jobLabels[job.kind] : tr("等待制作任务")}${clip ? `：${clip.title}` : ''}` : dependencies.length ? tr("等待前置片场：{{0}}", dependencies.map((s) => s!.title).join('、')) : qualityWait ? tr("等待总导演处理：{{0}}", nextClip?.title) : voices.length ? tr("等待固定音色：{{0}}", voices.map((asset) => asset!.name).join('、')) : actor.status === 'waiting' ? tr("等待新的结果或导演反馈") : actor.summary || tr("正在处理当前任务")
  const target = job?.query_outage_since ? tr("等待服务连接恢复后自动核对原任务，已提交素材与任务编号保留") : deferredCheck ? tr("检查暂时未成功，后台会继续同一检查；已有视频和已完成的分析保留") : job ? job.kind === 'reference'
    ? job.status === 'queued' ? tr("等待开始准备参考素材") : job.status === 'ambiguous' ? tr("参考素材准备结果待核实") : tr("正在本地准备参考素材")
    : job.kind === 'analysis' ? job.status === 'queued' ? tr("等待检查队列，使用已有视频进行分析") : tr("正在检查已有视频")
    : job.status === 'queued' ? tr("等待生成服务接收任务") : job.status === 'submitting' ? tr("正在向生成服务提交") : job.status === 'ambiguous' ? tr("提交结果待核实") : tr("{{0}}服务返回结果", jobLabels[job.kind])
    : dependencies.length ? tr("生成等待前置片场；当前规划可继续") : qualityWait ? tr("总导演比较候选后追加方案或明确选用，原检查记录保留") : voices.length ? tr("从正式发声片段提取固定音色，准备好后继续") : tr("等待外部结果或其他导演反馈")
  return { label, target, since: job?.query_outage_since ?? job?.created_at ?? actor.status_since, job, retryAt: deferredCheck && Number.isFinite(job.next_poll_at) ? job.next_poll_at * 1000 : null, lastProgress: job?.last_query_success_at ?? actor.last_progress_at }
}
export function elapsedLabel(since: string | null | undefined, now = Date.now()) {
  if (!since) return null
  const stamp = Date.parse(since)
  if (!Number.isFinite(stamp)) return null
  const seconds = Math.max(0, Math.floor((now - stamp) / 1000))
  if (seconds < 60) return tr("不足 1 分钟")
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return tr("{{0}} 分钟", minutes)
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return tr("{{0}} 小时 {{1}} 分钟", hours, minutes % 60)
  return tr("{{0}} 天 {{1}} 小时", Math.floor(hours / 24), hours % 24)
}
function useClock(active: boolean) {
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setClock(Date.now()), 15000)
    return () => window.clearInterval(timer)
  }, [active])
  return clock
}
export function NodeTask({ project, actor }: { project: StudioProject; actor: Actor }) {
  useLocale()

  const production = useProductionStatus()
  const current = production?.actors?.find((item) => item.actor_id === actor.id)
  const active = ['waiting', 'running', 'blocked', 'ready'].includes(actor.status) && !actor.paused
  const clock = useClock(active)
  if (!active) return null
  const task = taskSummary(project, actor)
  const elapsed = elapsedLabel(task.since, clock)
  return <div className="studio-node-task"><span><EntityText interactive={false}>{trSystem(current?.label ?? task.label)}</EntityText></span>{elapsed && (task.job || actor.status === 'waiting') && <small>{tr("已等待")} {elapsed}</small>}</div>
}
export function ActorWaitDetails({ project, actor, compact = false }: { project: StudioProject; actor: Actor; compact?: boolean }) {
  useLocale()

  const production = useProductionStatus()
  const current = production?.actors?.find((item) => item.actor_id === actor.id)
  const blockers = production?.blockers?.filter((item) => current?.blocker_keys?.includes(item.key)) ?? []
  const recovery = actor.image_input_recoveries?.at(-1)
  const recovering = recovery?.status === 'retrying' || recovery?.status === 'manual'
  const active = actor.status === 'waiting' || actor.status === 'running' || actor.status === 'blocked' || actor.status === 'ready' && recovering
  const clock = useClock(active)
  if (!active) return null
  const task = taskSummary(project, actor)
  const elapsed = elapsedLabel(task.since, clock)
  const last = task.lastProgress && Number.isFinite(Date.parse(task.lastProgress)) ? new Date(task.lastProgress).toLocaleString(getLocale()) : null
  const retry = task.retryAt === null ? null : task.retryAt > clock ? new Date(task.retryAt).toLocaleString(getLocale()) : tr("已到恢复时间，等待检查队列")
  return <details className="studio-wait-details"><summary><EntityText interactive={false}>{compact && !recovering ? tr("任务进展与等待原因") : trSystem(current?.label ?? task.label)}</EntityText>{elapsed ? tr(" · 已等待 {{0}}", elapsed) : ''}</summary><dl><div><dt>{tr("当前进展")}</dt><dd><EntityText>{trSystem(current?.label ?? task.target)}</EntityText></dd></div>{blockers.map((blocker) => <div key={blocker.key}><dt>{tr("等待条件")}</dt><dd><EntityText>{trSystem(blocker.description)}</EntityText></dd></div>)}{retry && <div><dt>{tr("下次自动尝试")}</dt><dd>{retry}</dd></div>}<div><dt>{tr("最近进展")}</dt><dd>{last ?? tr("尚无可用的进展时间记录")}</dd></div>{task.job?.error && <div><dt>{tr("任务说明")}</dt><dd><EntityText>{trSystem(task.job.error)}</EntityText></dd></div>}</dl>{!compact && <p><EntityText>{trSystem(actor.summary)}</EntityText></p>}</details>
}
