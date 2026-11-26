import { tr, trSystem, useLocale } from '../../ui/locale'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Background, BackgroundVariant, BaseEdge, Controls, Handle, Position, ReactFlow, useStore, useUpdateNodeInternals,
  type EdgeProps, type NodeProps, type ReactFlowInstance } from '@xyflow/react'
import { BookOpen, ChevronDown, ChevronUp, Crosshair, Film, GitBranch, Grid3X3, GripHorizontal, Maximize2, RotateCcw } from 'lucide-react'
import { type StudioClient } from './client'
import { useStudioImagePreviewUrl } from './image-preview'
import { GlobalMemoryNode } from './GlobalMemoryNode'
import { GlobalMemory } from './GlobalMemory'
import { SetPreview } from './SetPreview'
import { FullscreenControl, useStudioFullscreen } from './Fullscreen'
import { NodeTask } from './RuntimeSummary'
import { EntityText } from './EntityLinks'
import { activeClips, type Selection, type StudioProject, type WorkspaceLayout } from './model'
import { buildStudioProjection, STUDIO_CARD_HEIGHT, STUDIO_CARD_WIDTH, type NodeMeasurements, type StudioNode } from './projection'
import { routeWorkspacePoints, type Point } from './canvasGeometry'
import { mergeLayout, readCanvasGrid, readViewport, useWorkspaceLayout, writeCanvasGrid, writeViewport } from './workspace'
import { GLOBAL_STORY_LAYOUT_ID } from './memory-story-resize'
import { movedNodePatch, topLevelSelection } from './canvasSelection'
import { useMiddleSelection } from './useMiddleSelection'
import { workspaceEdgeObstacles } from './workspaceRouting'
import { useDependencyRouting } from './useDependencyRouting'
import { applyDependencyPorts } from './dependencyRoutePlan'
import { stableCanvasNodes } from './stableCanvasNodes'
import { Status } from './Status'
import '@xyflow/react/dist/style.css'
import './CanvasWorkspace.css'

function Ports() {
  useLocale()

  return <><Handle id="left" type="target" position={Position.Left} /><Handle id="right" type="source" position={Position.Right} />
    <Handle id="top" type="target" position={Position.Top} /><Handle id="bottom" type="source" position={Position.Bottom} /></>
}
function NodeCard({ data, showPorts = true, expandable = false }: Pick<NodeProps<StudioNode>, 'data'> & { showPorts?: boolean; expandable?: boolean }) {
  useLocale()

  const { selection } = data
  const imagePreviewUrl = useStudioImagePreviewUrl()
  const actor = selection.kind === 'actor' ? data.project?.actors?.[selection.id] : undefined
  return <div className={`studio-node studio-node--${selection.kind}${expandable ? ' is-expandable' : ''}`} style={{ width: STUDIO_CARD_WIDTH, height: STUDIO_CARD_HEIGHT }}>
    {showPorts && <Ports />}
    <div className="studio-node-grip" title={tr("拖动{{0}}", data.title)}><GripHorizontal size={16} /><span>{selection.kind === 'actor' ? tr("导演") : selection.kind === 'clip' ? tr("片段") : tr("记忆")}</span></div>
    <button className="studio-node-main nodrag" onClick={(event) => { event.stopPropagation(); data.onSelect?.(selection) }} aria-label={tr("打开{{0}}，{{1}}", data.title, data.eyebrow)}>
      <div className="studio-node-heading"><span title={data.eyebrow}>{data.eyebrow}</span>{data.status && <Status value={data.status} />}</div>
      <strong title={data.title}>{selection.kind === 'memory' && <BookOpen size={17} aria-hidden="true" />}<span>{data.displayTitle ?? data.title}</span></strong>
      <div className={`studio-node-preview${data.thumbnail ? ' has-thumbnail' : ''}`}>
        {data.thumbnail && <img className="studio-node-frame" src={imagePreviewUrl(data.thumbnail)} alt={tr("{{0}} 末帧", data.title)} loading="lazy" />}
        {actor && data.project && !actor.paused && ['waiting', 'running', 'blocked', 'ready'].includes(actor.status) ? <NodeTask project={data.project} actor={actor} /> : <p><EntityText interactive={false}>{data.description ? trSystem(data.description) : tr("等待导演开始工作")}</EntityText></p>}
      </div>
      <footer><span className="studio-node-progress">{data.progress}</span>{data.systemFallbackSelection ? <span className="studio-temporary-selection">{tr("系统兜底选用")}</span> : data.compromiseSelection ? <span className="studio-temporary-selection">{tr("总导演妥协选用")}</span> : data.temporarySelection && <span className="studio-temporary-selection">{tr("临时选用 · 未通过检查")}</span>}</footer>
    </button>
    {expandable && <button className="studio-set-toggle studio-node-toggle nodrag" aria-expanded={false} aria-label={tr("展开{{0}}", data.title)} title={tr("展开记忆与 Clip")} onClick={(event) => { event.stopPropagation(); data.onToggle?.(data.setId!) }}><ChevronDown size={17} /></button>}
  </div>
}
function SetGroup({ id, data }: NodeProps<StudioNode>) {
  useLocale()

  const updateInternals = useUpdateNodeInternals()
  const measured = useStore((state) => Boolean(state.nodeLookup.get(id)?.internals.handleBounds))
  const portSignature = data.ports?.map((port) => `${port.id}:${port.side}:${port.offset}`).join('|')
  useEffect(() => { if (measured && portSignature) updateInternals(id) }, [id, measured, portSignature, updateInternals])
  return <div className={`studio-set-group${data.expanded ? ' is-expanded' : ''}`}>
    {data.ports?.map((port) => <Handle key={port.id} id={port.id} type={port.type} position={port.side as Position} className="studio-set-port"
      style={port.side === 'left' || port.side === 'right' ? { top: `${port.offset}%` } : { left: `${port.offset}%` }} />)}
    {!data.expanded ? <NodeCard data={data} showPorts={false} expandable /> : <div className="studio-set-drag" title={tr("拖动整个片场：{{0}}", data.title)}><GripHorizontal size={16} /><span>{data.eyebrow.split(' · ')[0]}</span><strong>{data.displayTitle ?? data.title}</strong>
      <button className="studio-set-toggle nodrag" aria-expanded={data.expanded} aria-label={`${data.expanded ? tr("收起") : tr("展开")}${data.title}`} onClick={(event) => { event.stopPropagation(); data.onToggle?.(data.setId!) }}>
        {data.expanded ? <ChevronUp size={17} /> : <ChevronDown size={17} />}
      </button>
    </div>}
  </div>
}
function SetPreviewNode({ data }: NodeProps<StudioNode>) {
  useLocale()

  return <div className="studio-node studio-node--set-preview" style={{ width: STUDIO_CARD_WIDTH, height: STUDIO_CARD_HEIGHT }}>
    <div className="studio-node-grip" title={tr("拖动{{0}}", data.title)}><GripHorizontal size={16} /><span>{tr("连播")}</span></div>
    {data.project && data.setId && <SetPreview key={`${data.project.id}:${data.setId}`} compact project={data.project} setId={data.setId} client={data.client} onSelect={data.onSelect} />}
  </div>
}
function DynamicWorkspaceEdge(props: EdgeProps) {
  useLocale()

  const nodes = useStore((state) => state.nodes)
  const points = useMemo(() => routeWorkspacePoints({ x: props.sourceX, y: props.sourceY }, { x: props.targetX, y: props.targetY }, workspaceEdgeObstacles(nodes, props),
    { sourceSide: props.sourcePosition, targetSide: props.targetPosition, lane: typeof props.data?.lane === 'number' ? props.data.lane : 0 }),
  [nodes, props.id, props.source, props.sourceX, props.sourceY, props.targetX, props.targetY, props.sourcePosition, props.targetPosition, props.data?.lane])
  return <WorkspaceEdgePath {...props} points={points} />
}
function WorkspaceEdgePath(props: EdgeProps & { points: Point[] }) {
  useLocale()

  const { points } = props
  const path = points.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' ')
  return <g><title>{typeof props.data?.description === 'string' ? props.data.description : ''}</title>
    {(props.data?.crossingGaps as Point[][] | undefined)?.map((gap, i) => <path key={i}
      d={gap.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' ')}
      className="studio-edge-crossing-gap" aria-hidden="true" />)}
    <BaseEdge id={props.id} path={path} markerEnd={props.markerEnd} style={props.style} /></g>
}
// Worker-routed dependencies never subscribe to the entire moving node store.
const WorkspaceEdge = memo((props: EdgeProps) => props.data?.route
  ? <WorkspaceEdgePath {...props} points={props.data.route as Point[]} /> : <DynamicWorkspaceEdge {...props} />)
const nodeTypes = { studio: memo(NodeCard), setGroup: memo(SetGroup), globalMemory: memo(GlobalMemoryNode), setPreview: memo(SetPreviewNode) }
const edgeTypes = { workspace: WorkspaceEdge }
function useMobile() {
  const [mobile, setMobile] = useState(() => window.matchMedia?.('(max-width: 760px)').matches ?? false)
  useEffect(() => {
    const query = window.matchMedia?.('(max-width: 760px)')
    if (!query) return
    const update = () => setMobile(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return mobile
}
const layoutLabels = { get loading() { return tr("正在载入布局") }, get saved() { return tr("布局已保存") }, get dirty() { return tr("布局待保存") }, get saving() { return tr("正在保存布局") }, get error() { return tr("布局未保存") }, get conflict() { return tr("布局有冲突") }, get local() { return tr("当前窗口布局") } }

export function StudioCanvas({ project, selection, expanded, onSelect, onToggle, client }: {
  project: StudioProject
  selection: Selection | null
  expanded: ReadonlySet<string>
  onSelect: (selection: Selection) => void
  onToggle: (setId: string) => void
  client?: StudioClient
}) {
  const locale = useLocale()

  const projectId = project.id ?? ''
  const [dependencies, setDependencies] = useState(true)
  const [gridPreference, setGridPreference] = useState(() => ({ projectId, visible: readCanvasGrid(projectId) }))
  const gridVisible = gridPreference.projectId === projectId ? gridPreference.visible : readCanvasGrid(projectId)
  const [measurements, setMeasurements] = useState<NodeMeasurements>({})
  const [positions, setPositions] = useState<NonNullable<WorkspaceLayout['nodes']>>({})
  const workspace = useWorkspaceLayout(projectId, client)
  const flow = useRef<ReactFlowInstance<StudioNode> | null>(null)
  const surface = useRef<HTMLDivElement>(null)
  const dragging = useRef(new Set<string>())
  const dragOrigins = useRef(new Map<string, { x: number; y: number }>())
  const resizing = useRef<string | null>(null)
  const keyboardMove = useRef(new Set<string>())
  const mobile = useMobile()
  const fullscreen = useStudioFullscreen()
  const underlay = useRef<HTMLDivElement>(null)
  useEffect(() => { if (underlay.current) underlay.current.inert = fullscreen?.mode === 'memory' }, [fullscreen?.mode])
  const layout = useMemo(() => mergeLayout(workspace.layout, { nodes: positions }), [workspace.layout, positions])
  const projection = useMemo(() => buildStudioProjection(project, expanded, dependencies, layout, measurements, true), [project, expanded, dependencies, layout, measurements, locale])
  const routing = useDependencyRouting(projection.nodes, projection.edges)
  const graph = useMemo(() => ({ nodes: applyDependencyPorts(projection.nodes, projection.edges, routing.routes), edges: projection.edges }), [projection, routing.routes])
  const graphRef = useRef(graph)
  graphRef.current = graph
  const inspectorKey = selection ? `${selection.kind}:${selection.id}` : ''
  const [choice, setChoice] = useState<{ projectId: string; inspectorKey: string; ids: Set<string> | null }>({ projectId, inspectorKey, ids: null })
  const selectedIds = useMemo(() => choice.projectId === projectId && choice.inspectorKey === inspectorKey && choice.ids !== null
    ? topLevelSelection(choice.ids, graph.nodes)
    : topLevelSelection(new Set(graph.nodes.filter((node) => node.type !== 'setGroup' && selection?.kind === node.data.selection.kind && selection.id === node.data.selection.id).map((node) => node.id)), graph.nodes),
  [choice, projectId, inspectorKey, graph.nodes, selection])
  const pickNodes = useCallback((ids: Set<string>) => {
    setChoice({ projectId, inspectorKey, ids: topLevelSelection(ids, graphRef.current.nodes) })
  }, [projectId, inspectorKey])
  const openSelection = useCallback((value: Selection) => {
    pickNodes(new Set(graphRef.current.nodes.filter((node) => node.type !== 'setGroup' && node.data.selection.kind === value.kind && node.data.selection.id === value.id).map((node) => node.id)))
    onSelect(value)
  }, [pickNodes, onSelect])
  const boxSelection = useMiddleSelection({ projectId, enabled: !mobile && workspace.status !== 'loading', surface, flow, selected: selectedIds, onSelect: pickNodes })
  useEffect(() => {
    setChoice((previous) => previous.projectId === projectId && previous.inspectorKey === inspectorKey ? previous : { projectId, inspectorKey, ids: null })
  }, [projectId, inspectorKey])
  useEffect(() => { setPositions({}); setMeasurements({}); dragging.current.clear(); dragOrigins.current.clear() }, [projectId])
  useEffect(() => { if (['saved', 'loading'].includes(workspace.status)) setPositions({}) }, [workspace.status, workspace.layout.version])
  const updateStoryWidth = useCallback((width: number | null) => workspace.updateNodes({ [GLOBAL_STORY_LAYOUT_ID]: { x: 0, y: 0, width, height: null } }), [workspace.updateNodes])
  const updateMemorySize = useCallback((width: number, height: number | null) => {
    const id = 'memory:global'
    const position = flow.current?.getNode(id)?.position ?? graphRef.current.nodes.find((node) => node.id === id)?.position ?? { x: 0, y: 0 }
    workspace.updateNodes({ [id]: { ...position, width, height } })
    resizing.current = null
    setPositions((previous) => Object.fromEntries(Object.entries(previous).filter(([key]) => key !== id)))
  }, [workspace.updateNodes])
  const previousNodes = useRef<StudioNode[]>([])
  const nodes = useMemo(() => {
    const next: StudioNode[] = graph.nodes.map((node) => ({
    ...node, draggable: node.draggable && workspace.status !== 'loading',
    measured: node.type === 'globalMemory' ? measurements[node.id] : { width: node.width!, height: node.height! },
    selectable: true, selected: selectedIds.has(node.id),
    data: { ...node.data, project, client, onSelect: openSelection, onToggle, selectedClipId: selection?.kind === 'clip' ? selection.id : undefined, assetOrder: workspace.layout.asset_order,
      onAssetOrderChange: workspace.updateAssetOrder,
      storyWidth: workspace.layout.nodes?.[GLOBAL_STORY_LAYOUT_ID]?.width ?? null,
      onStoryWidthChange: updateStoryWidth,
      onSizeChange: node.type === 'globalMemory' ? updateMemorySize : undefined },
    }))
    previousNodes.current = stableCanvasNodes(previousNodes.current, next)
    return previousNodes.current
  }, [graph.nodes, measurements, selectedIds, project, client, openSelection, onToggle, workspace.layout.asset_order, workspace.layout.nodes, workspace.updateAssetOrder, updateStoryWidth, updateMemorySize, workspace.status])
  const edges = useMemo(() => graph.edges.filter((edge) => !edge.id.startsWith('dependency:') || routing.routes.has(edge.id)).map((edge) => {
    const route = routing.routes.get(edge.id)
    return route ? { ...edge, data: { ...edge.data, route: route.points, crossingGaps: routing.gaps.get(edge.id) } } : edge
  }), [graph.edges, routing])
  const clips = activeClips(project)
  const semanticNodes = nodes.filter((node) => node.type !== 'setGroup')
  const topLeft = {
    x: Math.min(0, ...nodes.filter((node) => !node.parentId).map((node) => node.position.x)),
    y: Math.min(0, ...nodes.filter((node) => !node.parentId).map((node) => node.position.y)),
  }
  const fit = (focus = false) => {
    const targets = focus ? nodes.filter((node) => node.selected || (['asset', 'reference'].includes(selection?.kind ?? '') && node.id === 'memory:global')) : nodes
    if (targets.length) void flow.current?.fitView({ nodes: targets, padding: 0.15, minZoom: 0.15, maxZoom: 1,
      duration: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 0 : 180 })
  }
  const nodePosition = (id: string, position: { x: number; y: number }) => {
    const node = graph.nodes.find((candidate) => candidate.id === id)
    return { ...workspace.layout.nodes?.[id], width: node?.type === 'globalMemory' ? node.width ?? null : null, height: node?.type === 'globalMemory' ? node.height ?? null : null,
      x: node?.parentId ? Math.max(16, position.x) : position.x, y: node?.parentId ? Math.max(64, position.y) : position.y }
  }
  const beginDrag = (items: StudioNode[]) => {
    const movers = topLevelSelection(items.map((node) => node.id), graph.nodes)
    dragging.current = movers
    dragOrigins.current = new Map(items.filter((node) => movers.has(node.id)).map((node) => [node.id, { ...node.position }]))
  }
  const finishDrag = (items: StudioNode[]) => {
    const patch = movedNodePatch(items, dragOrigins.current, workspace.layout.nodes ?? {})
    if (Object.keys(patch).length) workspace.updateNodes(patch)
    dragging.current.clear(); dragOrigins.current.clear(); setPositions({})
  }
  return <section className="studio-canvas studio-canvas-workspace" aria-label={tr("制作画布")} onKeyDownCapture={(event) => {
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)
      && event.target instanceof HTMLElement && event.target.classList.contains('react-flow__node')) {
      const id = event.target.dataset.id
      keyboardMove.current = new Set(id && selectedIds.has(id) ? selectedIds : id ? [id] : [])
    }
  }} onKeyUpCapture={() => { keyboardMove.current.clear() }}>
    <div className="studio-canvas-content" ref={underlay} aria-hidden={fullscreen?.mode === 'memory' ? true : undefined}>
    <div className="studio-canvas-toolbar"><div><Film size={18} /><h2>{tr("制作画布")}</h2><span>{semanticNodes.length} {tr("个节点")}</span></div><div>{mobile && <span className="studio-mobile-progress">{clips.filter((clip) => clip.status === 'accepted').length} / {clips.length} {tr("已选用")}</span>}
      {!mobile && <button className={`studio-button studio-button--quiet${dependencies ? ' is-active' : ''}`} onClick={() => setDependencies(!dependencies)} aria-pressed={dependencies} title={tr("编号表示成片顺序；连线表示生成依赖")}><GitBranch size={15} />{tr("片场依赖")}</button>}
      {!mobile && <button className={`studio-button studio-button--quiet${gridVisible ? ' is-active' : ''}`} aria-pressed={gridVisible} onClick={() => {
        writeCanvasGrid(projectId, !gridVisible); setGridPreference({ projectId, visible: !gridVisible })
      }}><Grid3X3 size={15} />{tr("背景网格")}</button>}
      {!mobile && <button className="studio-icon" aria-label={tr("聚焦所选节点")} disabled={!selection && !selectedIds.size} onClick={() => fit(true)}><Crosshair size={16} /></button>}
      {!mobile && <button className="studio-icon" aria-label={tr("适应画布")} onClick={() => fit()}><Maximize2 size={16} /></button>}
      {mobile && <FullscreenControl mode="memory" />}
      <FullscreenControl mode="canvas" />
    </div></div>
    <div className={`studio-layout-state studio-layout-state--${workspace.status}`}>
      <span role="status" aria-live="polite">{layoutLabels[workspace.status]}</span>
      {workspace.message && <span>{trSystem(workspace.message)}</span>}
      {workspace.status === 'error' && <button onClick={workspace.retry}>{tr("重试保存 / 载入")}</button>}
      {workspace.status === 'conflict' && <><button onClick={workspace.reload}>{tr("载入共享布局")}</button><button onClick={workspace.reapply}>{workspace.resetPending ? tr("按最新版本重置") : tr("合并我的调整")}</button></>}
      <button className="studio-layout-reset" disabled={['loading', 'saving', 'conflict'].includes(workspace.status)} onClick={() => { setPositions({}); workspace.reset() }}><RotateCcw size={12} />{tr("重置布局")}</button>
    </div>
    {mobile ? <div className="studio-mobile-outline" aria-label={tr("片场大纲")}>{semanticNodes.map((node) => <div key={node.id} className={`studio-outline-item studio-outline-item--${node.data.selection.kind}${node.data.setId ? ' is-set' : ''}`}>
      {node.type === 'setPreview' ? <SetPreview key={`${projectId}:${node.data.setId}`} compact project={project} setId={node.data.setId!} client={client} onSelect={onSelect} /> : <button onClick={() => onSelect(node.data.selection)}>
        <span className="studio-outline-kicker">{node.data.eyebrow}</span><strong>{node.data.displayTitle ?? node.data.title}</strong>
        {node.data.selection.kind === 'actor' && project.actors?.[node.data.selection.id]
          ? <NodeTask project={project} actor={project.actors[node.data.selection.id]} /> : <span className="studio-outline-description"><EntityText interactive={false}>{trSystem(node.data.description)}</EntityText></span>}
        <footer>{node.data.status && <Status value={node.data.status} />}{node.data.progress}{node.data.systemFallbackSelection ? <span className="studio-temporary-selection">{tr("系统兜底选用")}</span> : node.data.compromiseSelection ? <span className="studio-temporary-selection">{tr("总导演妥协选用")}</span> : node.data.temporarySelection && <span className="studio-temporary-selection">{tr("临时选用 · 未通过检查")}</span>}</footer>
      </button>}
      {node.data.selection.kind === 'actor' && node.data.setId && <button className="studio-outline-toggle" aria-expanded={node.data.expanded} onClick={() => onToggle(node.data.setId!)}>{node.data.expanded ? tr("收起片场") : tr("展开记忆与 Clip")}<ChevronDown size={16} /></button>}
    </div>)}</div> : <div ref={surface} className={`studio-flow-surface${boxSelection.marquee ? ' is-selecting' : ''}`} data-testid="studio-flow-surface" {...boxSelection.handlers}
      onMouseDownCapture={(event) => { if (event.button === 1) { event.preventDefault(); event.stopPropagation() } }}
      onAuxClickCapture={(event) => { if (event.button === 1) { event.preventDefault(); event.stopPropagation() } }}><ReactFlow<StudioNode>
      key={projectId} nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
      onInit={(instance) => { flow.current = instance }}
      onNodeClick={(_, node) => { if (node.type === 'studio') openSelection(node.data.selection) }}
      onPaneClick={() => pickNodes(new Set())}
      onNodeDragStart={(_, _node, items) => beginDrag(items)}
      onNodeDragStop={(_, _node, items) => finishDrag(items)}
      onSelectionDragStart={(_, items) => beginDrag(items)}
      onSelectionDragStop={(_, items) => finishDrag(items)}
      onNodesChange={(changes) => {
        const dimensions: NodeMeasurements = {}
        const moved: NonNullable<WorkspaceLayout['nodes']> = {}
        const keyMoves: NonNullable<WorkspaceLayout['nodes']> = {}
        const selectedChanges = changes.filter((change) => change.type === 'select')
        if (selectedChanges.length) {
          setChoice((previous) => {
            const next = new Set(previous.projectId === projectId && previous.inspectorKey === inspectorKey && previous.ids !== null ? previous.ids : selectedIds)
            for (const change of selectedChanges) { if (change.selected) next.add(change.id); else next.delete(change.id) }
            return { projectId, inspectorKey, ids: topLevelSelection(next, graph.nodes) }
          })
        }
        for (const change of changes) if (change.type === 'dimensions' && change.resizing) resizing.current = change.id
        for (const change of changes) {
          if (change.type === 'dimensions' && change.dimensions && !change.id.startsWith('set:')) {
            dimensions[change.id] = change.dimensions
            if (resizing.current === change.id) moved[change.id] = { ...nodePosition(change.id, graph.nodes.find((node) => node.id === change.id)?.position ?? { x: 0, y: 0 }), ...moved[change.id], ...change.dimensions }
          }
          if (change.type === 'position' && change.position && (change.dragging || dragging.current.has(change.id) || keyboardMove.current.has(change.id) || resizing.current === change.id)) {
            const position = nodePosition(change.id, change.position)
            moved[change.id] = { ...position, ...moved[change.id], x: position.x, y: position.y }
            if (keyboardMove.current.has(change.id)) keyMoves[change.id] = moved[change.id]
          }
        }
        if (Object.keys(keyMoves).length) workspace.updateNodes(keyMoves)
        if (Object.keys(dimensions).length) setMeasurements((previous) => {
          if (Object.entries(dimensions).every(([id, size]) => previous[id]?.width === size.width && previous[id]?.height === size.height)) return previous
          return { ...previous, ...dimensions }
        })
        if (Object.keys(moved).length) setPositions((previous) => ({ ...previous, ...moved }))
      }}
      defaultViewport={readViewport(projectId) ?? { x: 24 - topLeft.x * 0.72, y: 24 - topLeft.y * 0.72, zoom: 0.72 }}
      onMoveEnd={(_, viewport) => writeViewport(projectId, viewport)} minZoom={0.15} maxZoom={1.6}
      nodesDraggable={workspace.status !== 'loading'} nodesConnectable={false} elementsSelectable
      panOnScroll={!boxSelection.marquee} zoomOnScroll={!boxSelection.marquee} zoomOnPinch={!boxSelection.marquee}
      panOnDrag={boxSelection.marquee ? false : [0, 2]} selectionOnDrag={false} deleteKeyCode={null} autoPanOnNodeFocus={false} aria-label={tr("项目节点画布")}
    >{gridVisible && <Background variant={BackgroundVariant.Lines} gap={40} lineWidth={0.8} color="var(--studio-grid)" />}<Controls showInteractive={false} /></ReactFlow>
      {boxSelection.marquee && <div className="studio-marquee" data-testid="studio-marquee" aria-hidden="true" style={{ left: boxSelection.marquee.x, top: boxSelection.marquee.y, width: boxSelection.marquee.width, height: boxSelection.marquee.height }} />}
    </div>}
    {!mobile && <div className="studio-canvas-hint" aria-live="polite">{selectedIds.size > 1 ? tr("已框选 {{0}} 项，拖动任一卡片上沿一起移动；Esc 取消。", selectedIds.size) : tr("按住鼠标滚轮拖动框选；左键拖动画布，拖动标题移动卡片。")}</div>}
    </div>
    {fullscreen?.mode === 'memory' && <section className="studio-memory-fullscreen" aria-label={tr("全屏全局记忆")}>
      <header><h2><BookOpen size={22} aria-hidden="true" />{tr("全局记忆")}</h2><FullscreenControl mode="memory" /></header>
      <div className="studio-memory-fullscreen-scroll"><GlobalMemory fillHeight project={project} selectedClipId={selection?.kind === 'clip' ? selection.id : undefined} onSelect={onSelect} assetOrder={workspace.layout.asset_order ?? {}} onAssetOrderChange={workspace.updateAssetOrder}
        storyWidth={workspace.layout.nodes?.[GLOBAL_STORY_LAYOUT_ID]?.width ?? null} onStoryWidthChange={(width) => workspace.updateNodes({ [GLOBAL_STORY_LAYOUT_ID]: { x: 0, y: 0, width, height: null } })} /></div>
    </section>}
  </section>
}
