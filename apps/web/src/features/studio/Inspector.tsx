import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { ArrowLeft, BookOpen, Maximize2, Minimize2, Pause, Play, X } from 'lucide-react'
import { roleLabels, type Selection, type StudioProject } from './model'
import { Status } from './Status'
import { MediaPreview } from './Media'
import { AssetDetail, MemoryPanel } from './MemoryPanel'
import { DetailValue, Transcript } from './Transcript'
import { BodyReader, LazyDetails, useEntityDetail } from './data'
import { ActorWaitDetails } from './RuntimeSummary'
import { Markdown } from './Markdown'
import { EntityLinkProvider, EntityText } from './EntityLinks'
import { entityTarget } from './entity-link-model'
import { ReferencePlanPanel } from './ReferencePlanPanel'
import { MemoryEntryMetadata, memorySectionLabels, storySection } from './StoryMemory'
import { ClipPanel } from './ClipPanel'
import { FullscreenControl, useStudioFullscreen } from './Fullscreen'
import { ReadingText } from './ReadingText'
import { SetPreview } from './SetPreview'
import { SetDependencies } from './SetDependencies'
import { VisualTaskList, VisualTaskPanel } from './VisualAnalysis'

function inspectorPreference() {
  try {
    const value = JSON.parse(localStorage.getItem('studio:inspector') ?? '{}')
    return { width: typeof value.width === 'number' && Number.isFinite(value.width) ? Math.max(360, Math.min(1280, value.width)) : 460, wide: value.wide === true }
  } catch { return { width: 460, wide: false } }
}

type InspectorProps = {
  project: StudioProject; selection: Selection; busy: boolean; onClose: () => void; onBack?: () => void
  onSelect: (selection: Selection) => void; onControl: (actorId: string, action: 'pause' | 'resume') => void; onScreenplay: () => void
}
export function Inspector(props: InspectorProps) {
  useLocale()

  return props.selection.kind === 'entity' ? <EntityInspector {...props} /> : <InspectorView {...props} />
}
function EntityInspector(props: InspectorProps) {
  useLocale()

  const detail = useEntityDetail(props.project, `entity:${props.selection.id}`)
  const target = detail.loaded ? entityTarget(detail.project, props.selection.id) : null
  // Keep this one navigation frame: resolving an ID must not push a redirect onto
  // the inspector's return stack or substitute a currently selected candidate.
  return <InspectorView {...props} project={detail.project} selection={target && target.kind !== 'entity' ? target : props.selection}
    entityStatus={<>{detail.status}{detail.loaded && (!target || target.kind === 'entity') && <p>{tr("这条关联记录没有可显示的详情。")}</p>}</>} />
}
function InspectorView({ project, selection, busy, onClose, onSelect, onBack, onControl, onScreenplay, entityStatus }: InspectorProps & { entityStatus?: import('react').ReactNode }) {
  useLocale()

  const fullscreen = useStudioFullscreen()
  const actor = selection.kind === 'actor' ? project.actors?.[selection.id] : undefined
  const set = selection.kind === 'memory' || selection.kind === 'set-preview' ? project.sets?.[selection.id] : actor?.set_id ? project.sets?.[actor.set_id] : undefined
  const clip = selection.kind === 'clip' ? project.clips?.[selection.id] : undefined
  const title = actor ? roleLabels[actor.role] : clip ? clip.title : selection.kind === 'asset' ? project.assets?.[selection.id]?.name ?? tr("视觉资产") : selection.kind === 'reference' ? tr("参考图修订") : selection.kind === 'media' ? tr("生成素材") : selection.kind === 'change' ? tr("操作详情") : selection.kind === 'reference-plan' ? tr("参考清单") : selection.kind === 'story-event' ? tr("剧情事件") : selection.kind === 'entity' ? tr("关联记录") : selection.kind === 'visual-task' ? tr("看图任务") : selection.kind === 'set-preview' ? tr("片场连播") : selection.id === 'global' ? tr("全局记忆") : tr("片场记忆")
  const [preference, setPreference] = useState(inspectorPreference)
  const drag = useRef<{ x: number; width: number } | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { try { localStorage.setItem('studio:inspector', JSON.stringify(preference)) } catch { /* Local storage may be disabled. */ } }, [preference])
  useEffect(() => {
    const previous = document.activeElement
    if (window.matchMedia?.('(max-width: 760px)').matches) closeRef.current?.focus()
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus() }
  }, [])
  const setWidth = (width: number) => setPreference({ width: Math.max(360, Math.min(window.innerWidth - 60, 1280, width)), wide: false })
  return <EntityLinkProvider project={project} onSelect={onSelect}><aside className={`studio-inspector${preference.wide ? ' is-wide' : ''}`} style={{ '--inspector-width': `${preference.width}px` } as CSSProperties} aria-label={tr("{{0}}详情", title)} onKeyDown={(event) => { if (event.key === 'Escape') onClose() }}>
    <div className="studio-inspector-resize" role="separator" aria-label={tr("调整详情栏宽度")} aria-orientation="vertical" aria-valuemin={360} aria-valuemax={Math.max(360, Math.min(1280, window.innerWidth - 60))} aria-valuenow={preference.width} tabIndex={0}
      onPointerDown={(event) => { if (event.button !== 0) return; event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); drag.current = { x: event.clientX, width: event.currentTarget.parentElement!.getBoundingClientRect().width } }}
      onPointerMove={(event) => { if (drag.current) setWidth(drag.current.width + drag.current.x - event.clientX) }}
      onPointerUp={(event) => { drag.current = null; if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId) }}
      onPointerCancel={() => { drag.current = null }}
      onKeyDown={(event) => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); setWidth(preference.width + (event.key === 'ArrowLeft' ? 32 : -32)) } }} />
    <header className="studio-inspector-header">{onBack && <button className="studio-icon" onClick={onBack} aria-label={tr("返回上一详情")}><ArrowLeft size={18} /></button>}<div className="studio-inspector-heading"><span>{set?.title ?? project.title}</span><h2>{title}</h2></div>{fullscreen?.mode && <FullscreenControl mode={fullscreen.mode} className="studio-inspector-fullscreen" />}<button className="studio-icon studio-wide-toggle" onClick={() => setPreference((previous) => ({ ...previous, wide: !previous.wide }))} aria-label={preference.wide ? tr("恢复详情栏宽度") : tr("宽屏阅读")}>{preference.wide ? <Minimize2 size={18} /> : <Maximize2 size={18} />}</button><button ref={closeRef} className="studio-icon" onClick={onClose} aria-label={tr("关闭详情")}><X size={19} /></button></header>
    {actor ? <>
      <div className="studio-actor-toolbar"><Status value={actor.paused ? 'paused' : actor.status} />
        <div className="studio-actor-actions">{actor.status !== 'complete' && <button className="studio-button" disabled={busy} onClick={() => onControl(selection.id, actor.paused || actor.status === 'paused' ? 'resume' : 'pause')}>
          {actor.paused || actor.status === 'paused' ? <Play size={14} /> : <Pause size={14} />}{actor.paused || actor.status === 'paused' ? tr("继续") : tr("暂停")}</button>}
          {actor.role === 'screenwriter' && <button className="studio-button" onClick={onScreenplay}><BookOpen size={15} />{tr("打开剧本")}</button>}
        </div>
      </div>
      <Transcript project={project} actorId={selection.id} onSelect={onSelect} header={<section className="studio-actor-summary" aria-label={tr("当前任务")}>
        <h3>{tr("当前任务")}</h3>
        {actor.summary && <ReadingText key={actor.id} text={trSystem(actor.summary)} limit={180} lines={3} label={tr("任务说明")} />}
        {actor.last_error && <p className="studio-error-text"><EntityText>{actor.last_error}</EntityText></p>}
        <ActorWaitDetails project={project} actor={actor} compact />
        {actor.set_id && <SetDependencies project={project} setId={actor.set_id} onSelect={onSelect} />}
        {actor.role === 'executive' && <LazyDetails title={tr("委托看图任务")}><VisualTaskList project={project} actorId={actor.id!} onSelect={onSelect} /></LazyDetails>}
        {actor.role === 'visual_analyst' && actor.visual_task_id && <button className="studio-text-button" onClick={() => onSelect({ kind: 'visual-task', id: actor.visual_task_id! })}>{tr("查看任务与分析结论")}</button>}
        {actor.paused && <p className="studio-note">{tr("已提交的生成任务仍会完成并保存；继续后处理结果。")}</p>}
        {actor.set_id && <button className="studio-text-button" onClick={() => onSelect({ kind: 'memory', id: actor.set_id! })}><BookOpen size={15} />{tr("查看片场记忆")}</button>}
      </section>} />
    </> : <div className={`studio-inspector-scroll${selection.kind === 'clip' ? ' is-clip-detail' : ''}`}>
      {selection.kind === 'memory' ? selection.entryId ? <MemoryEntrySelection project={project} selection={selection} onSelect={onSelect} /> : <MemoryPanel key={selection.id} scope={selection.id} selectedClipId={selection.clipId} project={project} memory={selection.id === 'global' ? project.global_memory : set?.memory} global={selection.id === 'global'} onSelect={onSelect} />
        : selection.kind === 'clip' ? <ClipSelection project={project} selection={selection} onSelect={onSelect} />
        : selection.kind === 'set-preview' ? <SetPreview key={`${project.id}:${selection.id}`} project={project} setId={selection.id} onSelect={onSelect} />
        : selection.kind === 'asset' || selection.kind === 'reference' ? <AssetSelection project={project} selection={selection} onSelect={onSelect} onBack={onBack ?? (() => onSelect({ kind: 'memory', id: 'global' }))} />
        : selection.kind === 'media' ? <MediaSelection project={project} mediaId={selection.id} />
        : selection.kind === 'change' ? <ChangeSelection selection={selection} />
        : selection.kind === 'reference-plan' ? <ReferencePlanPanel project={project} planId={selection.id} onSelect={onSelect} />
        : selection.kind === 'story-event' ? <StoryEventSelection project={project} eventId={selection.id} />
        : selection.kind === 'visual-task' ? <VisualTaskPanel project={project} taskId={selection.id} onSelect={onSelect} />
        : selection.kind === 'entity' ? <div className="studio-clip-panel">{entityStatus}</div>
        : <div className="studio-empty"><p>{tr("内容已更新，请重新选择节点。")}</p></div>}
    </div>}
  </aside></EntityLinkProvider>
}

function MemoryEntrySelection({ project, selection, onSelect }: { project: StudioProject; selection: Selection; onSelect: (selection: Selection) => void }) {
  useLocale()

  const detail = useEntityDetail(project, `memory-entry:${selection.entryId}`)
  const memory = selection.id === 'global' ? detail.project.global_memory : detail.project.sets?.[selection.id]?.memory
  const entry = memory?.story?.find((item) => item.id === selection.entryId)
  const scope = selection.id === 'global' ? tr("全局记忆") : detail.project.sets?.[selection.id]?.title ?? tr("片场记忆")
  return <div className="studio-memory-panel studio-memory-record">{detail.status}{entry && <>
    <p className="studio-note">{scope} · {memorySectionLabels[storySection(entry)]}</p>
    <Markdown>{entry.text}</Markdown>
    <MemoryEntryMetadata entry={entry} />
    <button className="studio-text-button" onClick={() => onSelect({ kind: 'memory', id: selection.id })}>{tr("查看整份记忆")}</button>
  </>}{detail.loaded && !entry && <p className="studio-empty-line">{tr("这条记忆记录暂不可用。")}</p>}</div>
}

function StoryEventSelection({ project, eventId }: { project: StudioProject; eventId: string }) {
  useLocale()

  const detail = useEntityDetail(project, `story-event:${eventId}`)
  const event = detail.project.story_events?.[eventId]
  return <div className="studio-memory-panel studio-memory-record">{detail.status}{event && <><h3>{event.title}</h3><Markdown>{event.description ?? ''}</Markdown><p className="studio-note">{tr("按故事发生顺序定位；与成片顺序和媒体秒数分开记录。")}{event.story_clock ? tr("故事时钟：{{0}}", event.story_clock) : ''}</p>{event.certainty === 'unknown' && <p className="reference-plan-warning">{tr("此事件位置需要进一步核对。")}</p>}{(event.history?.length ?? 0) > 0 && <LazyDetails title={tr("事件修改历史")}>{event.history?.map((item, index) => <p key={index}>{String(item.title ?? '')}：{String(item.reason ?? '')}</p>)}</LazyDetails>}</>}</div>
}

function ClipSelection({ project, selection, onSelect }: { project: StudioProject; selection: Selection; onSelect: (selection: Selection) => void }) {
  useLocale()

  const detail = useEntityDetail(project, `clip:${selection.id}`)
  return <>{detail.status}{detail.project.clips?.[selection.id] ? <ClipPanel key={`${project.id}:${selection.id}:${selection.candidateId ?? ''}`} project={detail.project} clipId={selection.id} initialCandidateId={selection.candidateId} onSelect={onSelect} /> : <p className="studio-empty-line">{tr("正在定位这段视频的记录。")}</p>}</>
}
function AssetSelection({ project, selection, onSelect, onBack }: { project: StudioProject; selection: Selection; onSelect: (selection: Selection) => void; onBack: () => void }) {
  useLocale()

  const referenceId = selection.referenceId ?? (selection.kind === 'reference' ? selection.id : undefined)
  const detail = useEntityDetail(project, referenceId ? `reference:${referenceId}` : selection.stateId ? `state:${selection.stateId}` : undefined)
  const stateId = referenceId ? detail.project.references?.[referenceId]?.state_id : selection.stateId
  const assetId = selection.kind === 'asset' ? selection.id : stateId ? detail.project.states?.[stateId]?.asset_id : undefined
  const asset = assetId ? detail.project.assets?.[assetId] : undefined
  return <>{detail.status}{asset && assetId ? <AssetDetail project={detail.project} asset={{ ...asset, id: assetId }} highlightedStates={new Set()} initialStateId={stateId} initialReferenceId={referenceId} initialVoiceReferenceId={selection.voiceReferenceId} onBack={onBack} onSelect={onSelect} /> : <p className="studio-empty-line">{tr("正在定位这份资产记录。")}</p>}</>
}
function MediaSelection({ project, mediaId }: { project: StudioProject; mediaId: string }) {
  useLocale()

  const detail = useEntityDetail(project, `media:${mediaId}`)
  return <div className="studio-clip-panel">{detail.status}<MediaPreview media={detail.project.media?.[mediaId]} title={tr("生成素材")} /></div>
}
function ChangeSelection({ selection }: { selection: Selection }) {
  useLocale()

  const item = selection.item
  return <div className="studio-change-detail">{item ? <><p className="studio-note">{tr("保留该次操作的原始记录。")}</p>{item.body_ref ? <BodyReader bodyRef={item.body_ref} /> : <><Markdown>{item.text}</Markdown><LazyDetails title={tr("原始操作数据")}><DetailValue value={item.data} /></LazyDetails></>}</> : <p>{tr("未找到该次操作记录，请从导演对话重新打开。")}</p>}</div>
}
