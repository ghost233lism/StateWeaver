import { tr, trSystem, useLocale } from '../../ui/locale'
import { useRef, useState, type FormEvent } from 'react'
import { confirmsCommand, isDefiniteCommandRejection, operationKey, StudioApiError } from './client'
import type { Clip, ClipPreparationUpdate, StudioProject } from './model'
import { LazyDetails, useEntityDetail, useStudioData } from './data'
import { pendingCommands } from './pending-commands'
import { ReadingText } from './ReadingText'
import { EntityLink, EntityText } from './EntityLinks'
import { viewLabel } from './model'
import './ClipPreparation.css'

const statusLabels = { get draft() { return tr("可继续完善") }, get stale() { return tr("需要重新核对") }, get consumed() { return tr("已转入参考清单") }, get disabled() { return tr("已关闭") } }

export function ClipPreparationPanel({ project, clip }: { project: StudioProject; clip: Clip }) {
  useLocale()

  const frontier = (project.sets?.[clip.set_id]?.clip_ids ?? []).filter((id) => {
    const item = project.clips?.[id]
    return item && !item.archived_at && !item.accepted_candidate_id
  }).slice(0, 2)
  const editable = frontier.includes(clip.id ?? '') && !clip.archived_at && ['planned', 'repair', 'escalated', 'blocked'].includes(clip.status ?? 'planned')
  const preparation = clip.preparation
  if (!editable && !preparation) return null
  return <section className="studio-clip-preparation" aria-label={tr("本段预准备")}>
    <header><h3>{tr("本段预准备")}</h3><span>{preparation ? tr("第 {{0}} 版 · {{1}}", preparation.revision ?? 1, statusLabels[preparation.status ?? 'draft']) : tr("等待导演准备")}</span></header>
    {preparation?.action_notes && <ReadingText text={preparation.action_notes} limit={200} label={tr("接续要求")} />}
    {preparation && <>
      <dl className="studio-preparation-parts"><div><dt>{tr("剧情与素材")}</dt><dd>{preparation.stable_status === 'stale' ? tr("需要更新") : tr("可沿用")}</dd></div><div><dt>{tr("前段接续")}</dt><dd>{preparation.continuity_status === 'ready' ? tr("已对齐") : preparation.continuity_status === 'stale' ? tr("需要重新绑定") : tr("等待前段真实结果")}</dd></div></dl>
      {(preparation.dependency_changes?.length ?? 0) > 0 && <div className="studio-preparation-changes"><h4>{tr("变化与影响")}</h4>{preparation.dependency_changes?.map((change, index) => <PreparationChange key={index} change={change} />)}</div>}
    </>}
    {(preparation?.pending_requirements?.length ?? 0) > 0 && <p>{tr("待补齐：")}{preparation?.pending_requirements?.join('；')}</p>}
    {editable && <LazyDetails title={tr("调整预准备")}><PreparationForm project={project} clip={clip} /></LazyDetails>}
    {preparation?.prompt_draft && <LazyDetails title={tr("预准备提示词草稿")}><ReadingText text={preparation.prompt_draft} label={tr("提示词草稿")} /></LazyDetails>}
    {((clip.preparation_history?.length ?? 0) > 0 || (preparation?.revision ?? 0) > 1) && <LazyDetails title={tr("预准备修改记录")}><PreparationHistory project={project} clip={clip} /></LazyDetails>}
  </section>
}

function PreparationHistory({ project, clip }: { project: StudioProject; clip: Clip }) {
  useLocale()

  const detail = useEntityDetail(project, `preparation-history:${clip.id}`)
  const history = detail.project.clips?.[clip.id ?? '']?.preparation_history ?? clip.preparation_history
  return <>{detail.status}{history?.map((item) => <LazyDetails key={item.id} title={tr("第 {{0}} 版 · {{1}}", item.revision, statusLabels[item.status ?? 'draft'])}><p>{item.reason}</p><ReadingText text={item.action_notes ?? ''} label={tr("接续要求")} /><ReadingText text={item.prompt_draft ?? ''} label={tr("提示词草稿")} /></LazyDetails>)}</>
}

const dependencyFields: Record<string, string> = {
  get selected_revisions() { return tr("选用素材") }, get selected_revision() { return tr("选用版本") }, get asset_plan() { return tr("资产计划") }, get bindings() { return tr("参考绑定") },
  get accepted_candidate_id() { return tr("前段选用版本") }, get candidate_id() { return tr("前段选用版本") }, get id() { return tr("关联版本") }, get media_id() { return tr("素材文件") },
  get end_state() { return tr("真实末态") }, get end_frame_id() { return tr("真实末帧") }, get status() { return tr("准备状态") }, get description() { return tr("内容描述") },
  get starts_at() { return tr("剧情起点") }, get ends_at() { return tr("剧情终点") }, get start_state_id() { return tr("起始状态") }, get end_state_id() { return tr("结束状态") },
  get required_views() { return tr("所需视角") }, get text() { return tr("剧情要求") }, get version() { return tr("版本") }, get revision() { return tr("修订") }, get membership() { return tr("适用范围") },
  get selected_voice_id() { return tr("选用音色") }, get duration() { return tr("时长") }, get prompt_draft() { return tr("提示词草稿") }, get opening_description() { return tr("开场描述") },
  get previous_end_frame() { return tr("前段尾帧") }, get previous_tail() { return tr("前段尾视频") }, get previous_end_state() { return tr("前段末态") },
  get audio_references() { return tr("音频参考") }, get action_notes() { return tr("动作要求") },
  get camera() { return tr("机位与运镜") }, get space() { return tr("空间位置") }, get identity() { return tr("人物身份") }, get action() { return tr("动作阶段") },
  get frame_media_id() { return tr("末帧图片") }, get sha256() { return tr("素材内容") }, get source_sha256() { return tr("来源内容") },
  get request() { return tr("准备要求") }, get result() { return tr("准备结果") }, get source() { return tr("来源") }, get purpose() { return tr("用途") },
  get role() { return tr("参考类型") }, get voice() { return tr("音色") }, get attributes() { return tr("状态特征") },
}
function dependencyLabel(field: string): string {
  const parts = field.split('.')
  const labels = parts.flatMap((part, index) => dependencyFields[part] ? [dependencyFields[part]]
    : ['selected_revisions', 'required_views'].includes(parts[index - 1]) ? [viewLabel(part)] : [])
  return [...new Set(labels)].join(' · ') || tr("适用内容")
}
function PreparationChange({ change }: { change: Record<string, unknown> }) {
  useLocale()

  const entityId = typeof change.entity_id === 'string' ? change.entity_id : undefined
  const name = typeof change.label === 'string' ? change.label : change.component === 'continuity' ? tr("前段真实结果") : tr("剧情或素材")
  const fields = Array.isArray(change.changed_fields) ? change.changed_fields.map(String) : []
  const affected = Array.isArray(change.affected_fields) ? change.affected_fields.map(String) : []
  return <article>{entityId ? <EntityLink id={entityId}>{name}</EntityLink> : <strong>{name}</strong>}
    <p>{tr("变化：")}{[...new Set(fields.map(dependencyLabel))].join('、')}</p>
    {affected.length > 0 && <p>{tr("需核对：")}{affected.map(dependencyLabel).join('、')}</p>}
    {(change.before_revision || change.after_revision) ? <div className="studio-preparation-versions"><span>{tr("原版本")} <ReferenceVersions value={change.before_revision} /></span><span>{tr("当前版本")} <ReferenceVersions value={change.after_revision} /></span></div> : null}
    {change.previous_candidate_id || change.current_candidate_id ? <p>{typeof change.previous_candidate_id === 'string' && <EntityLink id={change.previous_candidate_id}>{tr("前段原选用结果")}</EntityLink>} → {typeof change.current_candidate_id === 'string' && <EntityLink id={change.current_candidate_id}>{tr("前段当前选用结果")}</EntityLink>}</p> : null}
    {typeof change.next_action === 'string' && <p className="studio-note"><EntityText>{trSystem(change.next_action)}</EntityText></p>}
  </article>
}
function ReferenceVersions({ value }: { value: unknown }) {
  useLocale()

  if (typeof value === 'string') return <EntityLink id={value}>{tr("查看版本")}</EntityLink>
  if (value && typeof value === 'object') return <>{Object.entries(value).filter(([, id]) => typeof id === 'string').map(([view, id]) => <EntityLink key={view} id={String(id)}>{viewLabel(view)}</EntityLink>)}</>
  return <>{tr("尚未选定")}</>
}

function PreparationForm({ project, clip }: { project: StudioProject; clip: Clip }) {
  useLocale()

  const source = useStudioData()
  const [fallbackCommands] = useState(() => pendingCommands())
  const commands = source?.pending ?? fallbackCommands
  const projectId = project.id, clipId = clip.id
  const target = [projectId ?? null, clipId ?? null]
  const recovered = commands.latest('preparation', target)
  const [baseId, setBaseId] = useState(recovered ? recovered.expected_preparation_id ?? null : clip.preparation?.id ?? null)
  const [enabled, setEnabled] = useState(recovered?.enabled ?? clip.preparation?.enabled ?? true)
  const [projectOverride, setProjectOverride] = useState(recovered?.prepare_next_clip ?? undefined)
  const [notes, setNotes] = useState(recovered?.action_notes ?? clip.preparation?.action_notes ?? '')
  const [prompt, setPrompt] = useState(recovered?.prompt_draft ?? clip.preparation?.prompt_draft ?? '')
  const [observed, setObserved] = useState<{ revision: number; preparation: Clip['preparation']; projectEnabled: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const saving = useRef(false)
  const useObserved = observed && observed.revision >= project.revision
  const current = useObserved ? observed.preparation : clip.preparation
  const projectEnabled = useObserved ? observed.projectEnabled : project.config?.prepare_next_clip ?? true
  const conflict = (current?.id ?? null) !== baseId
  const intent = [...target, enabled, notes, prompt, projectOverride ?? null]
  const attempted = commands.get('preparation', intent)
  const adopt = (preparation: Clip['preparation']) => {
    setBaseId(preparation?.id ?? null); setEnabled(preparation?.enabled ?? true)
    setProjectOverride(undefined)
    setNotes(preparation?.action_notes ?? ''); setPrompt(preparation?.prompt_draft ?? '')
  }
  const edit = () => { setMessage(''); setError('') }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const configure = source?.client.configurePreparation
    if (!configure || !projectId || !clipId || saving.current || (conflict && !attempted)) return
    const input: ClipPreparationUpdate = attempted ?? {
      expected_revision: Math.max(project.revision, observed?.revision ?? 0, source.currentCache?.()?.project.revision ?? 0),
      expected_preparation_id: baseId, idempotency_key: operationKey(),
      enabled, action_notes: notes, prompt_draft: prompt,
      prepare_next_clip: projectOverride ?? null,
    }
    const refresh = async () => {
      const latest = await source.client.details(projectId, `clip:${clipId}`)
      if (latest.project.id !== projectId) throw new Error("未能核对最新预准备，请重试。")
      const preparation = latest.project.clips?.[clipId]?.preparation
      setObserved({ revision: latest.project.revision, preparation, projectEnabled: latest.project.config?.prepare_next_clip ?? true })
      return preparation
    }
    saving.current = true
    setBusy(true); setError(''); setMessage('')
    let confirmed = false
    try {
      commands.save('preparation', intent, input)
      const result = await configure(projectId, clipId, input)
      if (!confirmsCommand(result, projectId)) throw new Error("未能确认预准备保存，请重试。")
      confirmed = true
      const updated = await refresh()
      commands.forget('preparation', intent, input.idempotency_key)
      adopt(updated)
      setMessage("已保存，导演会按最新状态核对。")
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请重试。")
      void source.checkMissingRoute?.(reason)
      if (!confirmed && isDefiniteCommandRejection(reason)) {
        commands.forget('preparation', intent, input.idempotency_key)
        if (reason instanceof StudioApiError && reason.status === 409) {
          try { await refresh() } catch (refreshError) { void source.checkMissingRoute?.(refreshError) }
        }
      }
    } finally { saving.current = false; setBusy(false) }
  }
  return <form className="studio-preparation-form" onSubmit={(event) => { void submit(event) }}>
    <label className="studio-preparation-toggle"><input type="checkbox" checked={projectOverride ?? projectEnabled} disabled={busy} onChange={(event) => { edit(); setProjectOverride(event.target.checked === projectEnabled ? undefined : event.target.checked) }} />{tr("本项目提前准备下一条 Clip")}</label>
    <label className="studio-preparation-toggle"><input type="checkbox" checked={enabled} disabled={busy} onChange={(event) => { edit(); setEnabled(event.target.checked) }} />{tr("提前准备本段")}</label>
    <p className="studio-note">{tr("正式生成前，导演会核对前段真实末态和最新素材。重新生成时绑定尾帧与尾 3 秒；编辑时使用完整候选并保留尾帧锚定。")}</p>
    <label>{tr("接续要求")}<textarea rows={3} value={notes} disabled={busy} onChange={(event) => { edit(); setNotes(event.target.value) }} /></label>
    <label>{tr("提示词草稿")}<textarea rows={7} value={prompt} disabled={busy} onChange={(event) => { edit(); setPrompt(event.target.value) }} /></label>
    {conflict && (attempted ? <p role="status">{tr("预准备已有新版。可先核对上次保存。")}</p> : <p role="alert">{tr("预准备已有新版。")}<button type="button" className="studio-text-button" disabled={busy} onClick={() => { adopt(current); edit() }}>{tr("加载新版")}</button></p>)}
    {error && <p role="alert">{trSystem(error)}</p>}{message && !conflict && <p role="status">{trSystem(message)}</p>}
    <button type="submit" className="studio-button studio-button--primary" disabled={busy || (conflict && !attempted) || !source?.client.configurePreparation}>{busy ? tr("正在保存…") : conflict && attempted ? tr("核对上次保存") : tr("保存预准备")}</button>
  </form>
}
