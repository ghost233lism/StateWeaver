import { tr, useLocale } from '../../ui/locale'
import { LazyDetails, useEntityHistory, useEntityDetail } from './data'
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, BookOpen, Box, Check, GitBranch } from 'lucide-react'
import { entries, revisionTree, viewLabel, referenceName, roleLabels, type Asset, type Reference, type Memory, type Selection, type StudioProject } from './model'
import { MediaPreview } from './Media'
import { Status } from './Status'
import { AssetPreview } from './AssetPreview'
import { briefStateDescription, StateEvolutionTree } from './StateEvolutionTree'
import { Markdown } from './Markdown'
import { AssetComparison } from './AssetComparison'
import { useMemoryEntries } from './StoryMemory'
import { StoryTimeline } from './StoryTimeline'
import { SetStoryMemory } from './SetStoryMemory'
import { EntityLinkProvider, EntityText } from './EntityLinks'
import { RepairDetails, SelectionNotice, VoiceReferences } from './ContinuityDetails'
import './MemoryWorkspace.css'
import { FullscreenControl, useStudioFullscreen } from './Fullscreen'

const kindLabels: Record<string, string> = { get character() { return tr("人物") }, get person() { return tr("人物") }, get object() { return tr("物体") }, get prop() { return tr("道具") }, get scene() { return tr("场景") }, get location() { return tr("场景") } }

export function MemoryPanel({ project, memory, global, onSelect, scope = 'global', selectedClipId }: {
  project: StudioProject; memory: Memory | undefined; global: boolean; scope?: string; selectedClipId?: string; onSelect: (selection: Selection) => void
}) {
  useLocale()

  const storyHistory = useMemoryEntries(project, scope)
  const fullscreen = useStudioFullscreen()
  const story = storyHistory.entries
  const [tab, setTab] = useState<'story' | 'assets'>('story')
  const [assetId, setAssetId] = useState<string | null>(null)
  const asset = assetId ? project.assets?.[assetId] : undefined
  const stateIds = new Set(memory?.asset_state_ids ?? [])
  const states = entries(project.states).filter((state) => stateIds.has(state.id))
  const assets = entries(project.assets).filter((candidate) => global || states.some((state) => state.asset_id === candidate.id))
  if (asset && assetId) return <AssetDetail project={project} asset={{ ...asset, id: assetId }} onBack={() => setAssetId(null)} onSelect={onSelect} highlightedStates={stateIds} />
  return <EntityLinkProvider project={project} onSelect={onSelect}><div className="studio-memory-panel">
    {global && fullscreen?.mode !== 'memory' && <div className="studio-memory-panel-controls"><FullscreenControl mode="memory" /></div>}
    <div className="studio-tabs" role="tablist" aria-label={tr("记忆类型")}>
      <button role="tab" aria-selected={tab === 'story'} onClick={() => setTab('story')}><BookOpen size={16} />{tr("剧情记忆")}<span>{memory?.story?.length ?? 0}</span></button>
      <button role="tab" aria-selected={tab === 'assets'} onClick={() => setTab('assets')}><Box size={16} />{tr("资产记忆")}<span>{assets.length}</span></button>
    </div>
    <div role="tabpanel" aria-label={tab === 'story' ? tr("剧情记忆") : tr("资产记忆")}>
      {tab === 'story' ? <>
        <div className="memory-reading-controls">{storyHistory.switcher}<span>{tr("修订")} {memory?.revision ?? 0}</span></div>
        {global ? <StoryTimeline project={project} records={story} selectedClipId={selectedClipId} /> : <SetStoryMemory project={project} setId={scope} entries={story} selectedClipId={selectedClipId} />}{storyHistory.controls}
      </> : <>
        <p className="studio-note">{project.catalog_frozen_at ? tr("目录已固定，各资产的剧情状态与参考图可继续更新。") : tr("总导演正在建立本片需要的视觉资产。")}</p>
        {assets.length ? <div className="studio-asset-grid">{assets.map((item) => <article key={item.id} className="studio-asset-card">
          <AssetPreview project={project} asset={item} onSelect={onSelect} />
          <button className="studio-asset-card-open" onClick={() => setAssetId(item.id)} aria-label={tr("查看资产{{0}}", item.name)}><span>{kindLabels[item.kind] ?? item.kind}</span><strong>{item.name}</strong><small>{tr("剧情状态与修订")}</small></button>
        </article>)}</div> : <div className="studio-empty"><Box size={28} /><h3>{tr("尚无资产")}</h3><p>{global ? tr("总导演会在准备阶段建立人物、道具和场景等视觉资产。") : tr("{{0}}绑定的资产状态会显示在这里。", roleLabels['set_director'])}</p></div>}
      </>}
    </div>
  </div></EntityLinkProvider>
}

type AssetDetailProps = {
  project: StudioProject
  asset: Asset & { id: string }
  onBack: () => void
  onSelect: (selection: Selection) => void
  highlightedStates: ReadonlySet<string>
  initialStateId?: string
  initialReferenceId?: string
  initialVoiceReferenceId?: string
}

export function AssetDetail(props: AssetDetailProps) {
  useLocale()

  return <AssetDetailSession key={`${props.asset.id}:${props.initialStateId ?? ''}:${props.initialReferenceId ?? ''}:${props.initialVoiceReferenceId ?? ''}`} {...props} />
}

function AssetDetailSession({ project: initialProject, asset, onBack, onSelect, highlightedStates, initialStateId, initialReferenceId, initialVoiceReferenceId }: AssetDetailProps) {
  useLocale()

  const surface = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const scroller = surface.current?.closest('.studio-inspector-scroll')
    if (scroller) scroller.scrollTop = 0
  }, [])
  const exact = useEntityDetail(initialProject, initialReferenceId ? `reference:${initialReferenceId}` : initialStateId ? `state:${initialStateId}` : undefined)
  const tree = useEntityDetail(exact.project, `asset-tree:${asset.id}`)
  const targetReference = initialReferenceId ? exact.project.references?.[initialReferenceId] : undefined
  const targetStateId = initialReferenceId ? targetReference?.state_id : initialStateId
  const [selectedState, setSelectedState] = useState<string>()
  const chosenStateId = selectedState ?? targetStateId
  const states = useMemo(() => entries(tree.project.states).filter((state) => state.asset_id === asset.id).sort((a, b) => a.story_time - b.story_time), [tree.project.states, asset.id])
  const hiddenStates = useMemo(() => new Set(Object.entries(tree.stateTree?.states ?? {}).filter(([, use]) => use.kind === 'process').map(([id]) => id)), [tree.stateTree])
  const pendingStates = useMemo(() => new Set(Object.entries(tree.stateTree?.states ?? {}).filter(([, use]) => use.kind === 'pending').map(([id]) => id)), [tree.stateTree])
  const videoStates = useMemo(() => new Set(Object.entries(tree.stateTree?.states ?? {}).filter(([, use]) => use.video_media_ids?.length).map(([id]) => id)), [tree.stateTree])
  const visibleStates = useMemo(() => states.filter((state) => !hiddenStates.has(state.id)), [states, hiddenStates])
  const hasExactTarget = Boolean(initialReferenceId || initialStateId)
  const state = states.find((candidate) => candidate.id === chosenStateId) ?? (hasExactTarget && !selectedState ? undefined : visibleStates.find((item) => item.is_base) ?? visibleStates.find((item) => highlightedStates.has(item.id)) ?? visibleStates[0])
  return <EntityLinkProvider project={tree.project} onSelect={onSelect}><div ref={surface} className="studio-asset-detail">
    <button className="studio-text-button" onClick={onBack}><ArrowLeft size={16} />{tr("返回资产记忆")}</button>
    <div className="studio-detail-title"><span>{kindLabels[asset.kind] ?? asset.kind}</span><h2>{asset.name}</h2><Markdown>{asset.description}</Markdown></div>
    {exact.status}
    <VoiceReferences project={tree.project} asset={asset} onSelect={onSelect} initialVoiceReferenceId={initialVoiceReferenceId} />
    <StateEvolutionTree project={tree.project} states={states} selectedId={state?.id} highlightedStates={highlightedStates} hiddenStates={hiddenStates} pendingStates={pendingStates} videoStates={videoStates} onSelect={setSelectedState} loading={tree.loading} notice={tree.loading || tree.error ? tree.status : undefined}>
      {!state && hasExactTarget && <p className="studio-empty-line">{tr("指定的剧情状态尚未加载。")}</p>}
      {state && <StateReferences key={state.id} project={tree.project} asset={asset} state={state} videoMediaIds={tree.stateTree?.states?.[state.id]?.video_media_ids} initialReferenceId={targetReference?.state_id === state.id ? initialReferenceId : undefined} onSelect={onSelect} />}
    </StateEvolutionTree>
  </div></EntityLinkProvider>
}

function StateReferences({ project: initialProject, asset, state, initialReferenceId, videoMediaIds, onSelect }: {
  project: StudioProject; asset: Asset & { id: string }; state: import('./model').AssetState & { id: string }; initialReferenceId?: string; videoMediaIds?: string[]; onSelect: (selection: Selection) => void
}) {
  useLocale()

  const history = useEntityHistory(initialProject, 'references', state.id)
  const project = history.project
  const references = entries(project.references).filter((reference) => reference.state_id === state.id)
  const visibleReferences = references.filter((reference) => !history.ids || history.ids.includes(reference.id) || history.ancestorIds.includes(reference.id) || reference.id === initialReferenceId)
  // Required views belong to the asset's evolving catalog. Later state-specific
  // additions are not missing image jobs for every earlier state.
  const views = Array.from(new Set([...Object.keys(state.selected_revisions ?? {}), ...references.map((reference) => reference.view)]))
  const ordinals = Object.fromEntries((history.items ?? []).flatMap((item) => typeof item.history_ordinal === 'number' ? [[String(item.id), item.history_ordinal]] : [])) as Record<string, number>
  const [compare, setCompare] = useState(false)
  return <EntityLinkProvider project={project} onSelect={onSelect}>
    <h3>{state.label}</h3>
    {briefStateDescription(state.description) && <p className="studio-state-description">{briefStateDescription(state.description)}</p>}
    <section className="studio-reference-views"><h3>{tr("参考素材")}</h3>
      {views.map((view) => {
        const selected = references.find((reference) => reference.id === state.selected_revisions?.[view])
        const media = selected?.media_id ? project.media?.[selected.media_id] : undefined
        return <article key={view}><header><h4>{viewLabel(view)}</h4>{selected && <span><Check size={13} />{selected.selection?.kind === 'system_fallback' ? tr("系统兜底选用") : selected.selection?.kind === 'director_compromise' ? tr("妥协选用") : tr("已选用")}</span>}</header><MediaPreview media={media} title={`${asset.name} ${state.label} ${viewLabel(view)}`} compact /></article>
      })}
      {videoMediaIds?.map((id) => <article key={id}><header><h4>{tr("视频参考")}</h4></header><MediaPreview media={project.media?.[id]} title={tr("{{0}} {{1}} 视频参考", asset.name, state.label)} compact /></article>)}
      {!views.length && !videoMediaIds?.length && <p className="studio-empty-line">{state.reference_purpose ? tr("参考素材正在准备。") : tr("此状态暂无参考图。")}</p>}
    </section>
    <section className="studio-history"><div className="memory-history-heading"><h3><GitBranch size={16} />{tr("修订历史树")}</h3><button className="studio-text-button" aria-expanded={compare} onClick={() => setCompare((value) => !value)}>{compare ? tr("收起比较") : tr("比较参考图")}</button></div><p className="studio-note">{tr("同一剧情状态的候选与修改保留在这里。")}</p>
      {compare && <AssetComparison project={project} asset={asset} state={state} references={references} initialReferenceId={initialReferenceId} ordinals={ordinals} />}
      {revisionTree(visibleReferences).map(({ reference, depth }) => {
        const ordinal = ordinals[reference.id]
        // A partial history page cannot establish a revision's global ordinal.
        const revisionLabel = referenceName(reference, ordinal)
        return <RevisionItem key={reference.id} project={project} asset={asset} reference={reference} depth={depth} revisionLabel={revisionLabel} selected={state.selected_revisions?.[reference.view] === reference.id} initiallyOpen={reference.id === initialReferenceId} onSelect={onSelect} />
      })}
      {history.controls}{!visibleReferences.length && <p className="studio-empty-line">{tr("生成参考图后，会在这里保留完整历史。")}</p>}
    </section>
  </EntityLinkProvider>
}

function RevisionItem({ project, asset, reference, depth, revisionLabel, selected, initiallyOpen, onSelect }: {
  project: StudioProject; asset: Asset & { id: string }; reference: Reference & { id: string }; depth: number; revisionLabel: string; selected: boolean; initiallyOpen: boolean; onSelect: (selection: Selection) => void
}) {
  useLocale()

  const [open, setOpen] = useState(initiallyOpen)
  return <div style={{ marginLeft: Math.min(depth, 4) * 14 }}><details className="studio-revision" open={open} data-reference-id={reference.id} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary onClick={(event) => { event.preventDefault(); setOpen((value) => !value) }}><span className="studio-tree-dot" /><div><strong>{viewLabel(reference.view)} · {revisionLabel}</strong><small><EntityText interactive={false}>{reference.reason || (reference.parent_id ? tr("基于已有参考图修改") : tr("首次生成"))}</EntityText></small></div>{selected ? <span className="memory-reference-selected"><Check size={13} />{tr("已选用")}</span> : <Status value={reference.status === 'selected' ? 'candidate' : reference.status} />}</summary>
    {open && <ReferenceBody project={project} asset={asset} referenceId={reference.id} onSelect={onSelect} />}
  </details></div>
}

function ReferenceBody({ project: initialProject, asset, referenceId, onSelect }: { project: StudioProject; asset: Asset & { id: string }; referenceId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const detail = useEntityDetail(initialProject, `reference:${referenceId}`)
  const project = detail.project
  const reference = project.references?.[referenceId]
  return <EntityLinkProvider project={project} onSelect={onSelect}><div className="studio-revision-body">{detail.status}{reference && <>
    <MediaPreview media={reference.media_id ? project.media?.[reference.media_id] : undefined} title={`${asset.name} ${referenceName(reference)}`} compact />
    <SelectionNotice value={reference} /><RepairDetails plan={reference.repair_plan} />
    {(reference.source_media_ids?.length ?? 0) > 0 && <LazyDetails title={tr("导演确认的画面细节")}>{reference.source_media_ids?.map((id) => <MediaPreview key={id} media={project.media?.[id]} title={tr("来自视频的已确认局部参考")} />)}</LazyDetails>}
    {(reference.review_history?.length ?? 0) > 0 && <LazyDetails title={tr("原始图片检查记录")}>{reference.review_history?.map((review, index) => <article key={index}><strong>{review.decision === 'accept' ? tr("检查通过") : tr("检查未通过")}</strong><Markdown>{typeof review.reason === 'string' ? review.reason : ''}</Markdown></article>)}</LazyDetails>}
    {(reference.source_revision_ids?.length ?? 0) > 0 && <LazyDetails title={tr("生成时参考的素材")}>{reference.source_revision_ids?.map((sourceId) => {
      const source = project.references?.[sourceId]
      const sourceState = source ? project.states?.[source.state_id] : undefined
      const sourceAsset = sourceState ? project.assets?.[sourceState.asset_id] : undefined
      return <article key={sourceId} className="studio-source-reference"><strong>{sourceAsset?.name ?? tr("参考资产")} · {sourceState?.label ?? tr("剧情状态")}</strong><span>{source ? viewLabel(source.view) : tr("参考修订")}</span><button className="studio-text-button" onClick={() => onSelect({ kind: 'reference', id: sourceId, referenceId: sourceId, stateId: source?.state_id })}>{tr("查看此参考修订")}</button></article>
    })}</LazyDetails>}
    {reference.reason && <LazyDetails title={tr("修订原因")}><Markdown>{reference.reason}</Markdown></LazyDetails>}
    {reference.prompt && <LazyDetails title={tr("完整生成要求")}><Markdown>{reference.prompt}</Markdown></LazyDetails>}
    {reference.parent_id && <button className="studio-text-button" onClick={() => onSelect({ kind: 'reference', id: reference.parent_id!, referenceId: reference.parent_id! })}>{tr("查看上一级修订")}</button>}
    <ReferenceUsage project={project} referenceId={referenceId} onSelect={onSelect} />
  </>}</div></EntityLinkProvider>
}

function ReferenceUsage({ project, referenceId, onSelect }: { project: StudioProject; referenceId: string; onSelect: (selection: Selection) => void }) {
  useLocale()

  const history = useEntityHistory(project, 'usage', referenceId)
  const candidates = entries(history.project.candidates).filter((candidate) => history.ids ? history.ids.includes(candidate.id) : candidate.bindings.some((binding) => binding.revision_id === referenceId))
  return <><h4>{tr("使用记录")}</h4>{candidates.length ? candidates.map((candidate) => <button key={candidate.id} className="studio-usage" onClick={() => onSelect({ kind: 'clip', id: candidate.clip_id, candidateId: candidate.id })}><span>{history.project.clips?.[candidate.clip_id]?.title ?? tr("片段")}</span><Status value={candidate.status} /></button>) : <p className="studio-empty-line">{tr("暂无使用记录。")}</p>}{history.controls}</>
}
