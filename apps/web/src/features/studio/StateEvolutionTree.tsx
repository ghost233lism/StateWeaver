import { tr, useLocale } from '../../ui/locale'
import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Expand, GitBranch, ImageOff, Maximize2, Minus, Plus, X } from 'lucide-react'
import { assetPreviewImages } from './AssetPreview'
import { useStudioImagePreviewUrl, useMediaVisible } from './image-preview'
import type { AssetState, StudioProject } from './model'
import { useStateTreeViewport } from './useStateTreeViewport'
import './StateEvolutionTree.css'

type State = AssetState & { id: string }
export type StateBranch = { state: State; children: StateBranch[]; missingParent?: string; invalid?: boolean; indirectVia?: string[] }
const noHiddenStates: ReadonlySet<string> = new Set()

/** Only recorded primary parents form edges. Story time orders siblings but
 * cannot create ancestry. Unloaded, cyclic or foreign parents stay explicit. */
export function stateEvolutionForest(states: State[], hiddenStates: ReadonlySet<string> = noHiddenStates): StateBranch[] {
  const nodes = new Map(states.map((state) => [state.id, { state, children: [] } as StateBranch]))
  const roots: StateBranch[] = []
  for (const node of nodes.values()) {
    const parentId = node.state.evolution?.kind === 'derived' ? node.state.evolution.parent_state_id : null
    const parent = parentId ? nodes.get(parentId) : undefined
    if (!parent) { if (parentId) node.missingParent = parentId; roots.push(node); continue }
    const seen = new Set([node.state.id])
    let ancestor: StateBranch | undefined = parent
    while (ancestor && !seen.has(ancestor.state.id) && ancestor.state.asset_id === node.state.asset_id) {
      seen.add(ancestor.state.id)
      const next: string | null | undefined = ancestor.state.evolution?.kind === 'derived' ? ancestor.state.evolution.parent_state_id : null
      ancestor = next ? nodes.get(next) : undefined
    }
    if (ancestor) { node.invalid = true; roots.push(node) } else parent.children.push(node)
  }
  const sort = (branches: StateBranch[]) => {
    branches.sort((a, b) => a.state.story_time - b.state.story_time || a.state.id.localeCompare(b.state.id))
    branches.forEach((branch) => sort(branch.children))
  }
  // Project the recorded ancestry without rewriting it. Only an actual chain
  // through hidden process nodes can create an indirect, dashed connection.
  const project = (branch: StateBranch, skipped: string[] = [], unresolved: Pick<StateBranch, 'missingParent' | 'invalid'> = {}): StateBranch[] => {
    const diagnostic = { missingParent: branch.missingParent ?? unresolved.missingParent, invalid: branch.invalid || unresolved.invalid }
    if (hiddenStates.has(branch.state.id)) return branch.children.flatMap((child) => project(child, [...skipped, branch.state.id], diagnostic))
    return [{ ...branch, ...diagnostic, indirectVia: skipped.length ? skipped : undefined, children: branch.children.flatMap((child) => project(child)) }]
  }
  const visible = roots.flatMap((root) => project(root))
  sort(visible)
  return visible
}


const nodeWidth = 188, nodeHeight = 174, columnGap = 64, rowGap = 34

/** A forest of actual primary parents. Each root gets its own vertical space. */
export function stateEvolutionLayout(states: State[], hiddenStates: ReadonlySet<string> = noHiddenStates) {
  const nodes: { branch: StateBranch; x: number; y: number }[] = []
  const links: { parent: string; child: string; path: string; indirect: boolean }[] = []
  let row = 0
  const place = (branch: StateBranch, depth: number): { x: number; y: number } => {
    const children = branch.children.map((child) => ({ child, point: place(child, depth + 1) }))
    const point = { x: 24 + depth * (nodeWidth + columnGap), y: children.length
      ? (children[0].point.y + children.at(-1)!.point.y) / 2 : 24 + row++ * (nodeHeight + rowGap) }
    nodes.push({ branch, ...point })
    for (const { child, point: end } of children) {
      const x = point.x + nodeWidth, y = point.y + nodeHeight / 2, mid = x + columnGap / 2
      links.push({ parent: branch.state.id, child: child.state.id, indirect: Boolean(child.indirectVia?.length),
        path: `M ${x} ${y} H ${mid} V ${end.y + nodeHeight / 2} H ${end.x}` })
    }
    return point
  }
  stateEvolutionForest(states, hiddenStates).forEach((root) => place(root, 0))
  nodes.sort((a, b) => a.branch.state.story_time - b.branch.state.story_time || a.branch.state.id.localeCompare(b.branch.state.id))
  return { nodes, links, width: Math.max(300, ...nodes.map((node) => node.x + nodeWidth + 24)),
    height: Math.max(240, ...nodes.map((node) => node.y + nodeHeight + 24)) }
}

/** A faithful short excerpt, not a new observed-state claim or an LLM rewrite. */
export function briefStateDescription(description: string | undefined) {
  const plain = (description ?? '').replace(/[#*_`]/g, '').replace(/\s+/g, ' ').trim()
  const sentences = plain.match(/[^。！？.!?]+[。！？.!?]?/g) ?? []
  const excerpt = sentences.slice(0, 2).join('').trim()
  return excerpt.length > 180 ? `${excerpt.slice(0, 180).trimEnd()}…` : excerpt
}

export function StateEvolutionTree({ project, states, selectedId, highlightedStates, onSelect, children, loading = false, notice, hiddenStates = noHiddenStates, pendingStates = noHiddenStates, videoStates = noHiddenStates }: {
  project?: StudioProject; states: State[]; selectedId?: string; highlightedStates: ReadonlySet<string>;
  onSelect: (id: string) => void; children?: ReactNode; loading?: boolean; notice?: ReactNode;
  hiddenStates?: ReadonlySet<string>; pendingStates?: ReadonlySet<string>; videoStates?: ReadonlySet<string>
}) {
  useLocale()

  const graph = useMemo(() => stateEvolutionLayout(states, hiddenStates), [states, hiddenStates])
  const dialog = useRef<HTMLDialogElement>(null)
  const fullscreenButton = useRef<HTMLButtonElement>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const geometryKey = graph.nodes.map((node) => `${node.branch.state.id}:${node.x}:${node.y}`).join('|')
  const { viewport, scene, zoom, fit, zoomBy, handlers } = useStateTreeViewport(graph, geometryKey)
  useEffect(() => {
    const element = dialog.current
    if (!element) return
    // The same DOM and detail session move into the browser's top layer. There
    // is no duplicate history request and no lost selection on exit.
    if (fullscreen) { element.close?.(); element.showModal?.(); element.setAttribute('open', '') }
    else { element.close?.(); element.setAttribute('open', '') }
  }, [fullscreen])
  const closeFullscreen = () => { setFullscreen(false); requestAnimationFrame(() => fullscreenButton.current?.focus()) }
  return <dialog ref={dialog} open className={`state-evolution-shell${fullscreen ? ' is-fullscreen' : ''}`} aria-label={tr("资产图片状态树")} role={fullscreen ? 'dialog' : 'group'}
    onCancel={(event) => { if (event.target !== event.currentTarget) return; event.preventDefault(); event.stopPropagation(); closeFullscreen() }} onKeyDown={(event) => { if (fullscreen && event.key === 'Escape') event.stopPropagation() }}>
    <header className="state-evolution-toolbar"><h3><GitBranch size={16} />{tr("图片状态树")} <span>{loading ? tr("正在加载…") : tr("{{0}} 个状态", graph.nodes.length)}</span></h3>
      <div><button type="button" title={tr("缩小")} aria-label={tr("缩小状态树")} disabled={zoom <= .2} onClick={() => zoomBy(1 / 1.25)}><Minus size={17} /></button>
        <output aria-label={tr("状态树缩放")}>{Math.round(zoom * 100)}%</output>
        <button type="button" title={tr("放大")} aria-label={tr("放大状态树")} disabled={zoom >= 2} onClick={() => zoomBy(1.25)}><Plus size={17} /></button>
        <button type="button" title={tr("查看完整分支")} aria-label={tr("适应状态树")} onClick={() => fit()}><Expand size={17} /></button>
        {!fullscreen && <button type="button" ref={fullscreenButton} title={tr("全屏")} aria-label={tr("全屏查看状态树")} onClick={() => setFullscreen(true)}><Maximize2 size={17} /></button>}
      </div>
    </header>
    {fullscreen && <button type="button" className="state-evolution-exit" aria-label={tr("退出状态树全屏")} title={tr("退出全屏（Esc）")} onClick={closeFullscreen}><X size={22} /></button>}
    <div className="state-evolution-content">
      <div ref={viewport} className="state-evolution-viewport nowheel nopan nodrag" role="region" aria-label={tr("状态演变树，滚轮缩放、拖动平移")} aria-busy={loading} tabIndex={0} {...handlers}>
        {notice && <div className="state-evolution-notice">{notice}</div>}
        <div ref={scene} className="state-evolution-scene" style={{ width: graph.width, height: graph.height }}>
          <StateScene graph={graph} project={project} selectedId={selectedId} highlightedStates={highlightedStates} pendingStates={pendingStates} videoStates={videoStates} onSelect={onSelect} />
        </div>
        {!graph.nodes.length && !loading && !notice && <p className="studio-empty-line">{tr("尚无需要参考的资产状态。")}</p>}
      </div>
      {children && <div className="state-evolution-inspector">{children}</div>}
    </div>
  </dialog>
}

const StateScene = memo(function StateScene({ graph, project, selectedId, highlightedStates, pendingStates, videoStates, onSelect }: {
  graph: ReturnType<typeof stateEvolutionLayout>; project?: StudioProject; selectedId?: string;
  highlightedStates: ReadonlySet<string>; pendingStates: ReadonlySet<string>; videoStates: ReadonlySet<string>; onSelect: (id: string) => void
}) {
  useLocale()

  return <>
    <svg width={graph.width} height={graph.height} aria-hidden="true">{graph.links.map((link) => <path key={link.child} d={link.path} data-parent-state={link.parent} data-child-state={link.child} data-indirect={link.indirect || undefined}><title>{link.indirect ? tr("间接演变") : tr("直接演变")}</title></path>)}</svg>
    {graph.nodes.map(({ branch, x, y }) => <StateImageNode key={branch.state.id} project={project} branch={branch} x={x} y={y}
      selected={selectedId === branch.state.id} current={highlightedStates.has(branch.state.id)} pending={pendingStates.has(branch.state.id)} video={videoStates.has(branch.state.id)} onSelect={onSelect} />)}
  </>
})

const StateImageNode = memo(function StateImageNode({ project, branch, x, y, selected, current, pending, video, onSelect }: {
  project?: StudioProject; branch: StateBranch; x: number; y: number; selected: boolean; current: boolean; pending: boolean; video: boolean; onSelect: (id: string) => void
}) {
  useLocale()

  const { state } = branch
  const image = project ? assetPreviewImages(project, state.asset_id, state.id)[0] : undefined
  const { ref, visible } = useMediaVisible()
  const [failed, setFailed] = useState('')
  const imagePreviewUrl = useStudioImagePreviewUrl()
  const url = image ? imagePreviewUrl(image.media.url, 256) : ''
  const status = branch.invalid ? tr("关系待核对") : branch.missingParent ? tr("父状态未载入") : !state.evolution ? tr("来源未记录")
    : state.evolution.kind === 'root' ? tr("初始状态") : state.evolution.kind === 'unresolved' ? tr("来源待确认") : current ? tr("当前记忆引用") : tr("衍生状态")
  return <button ref={ref} className="state-evolution-node" data-state-id={state.id} style={{ left: x, top: y, width: nodeWidth, height: nodeHeight }}
    aria-label={state.label} aria-pressed={selected} onClick={() => onSelect(state.id)}>
    <span className="state-evolution-picture">{url && failed !== url ? <img src={visible ? url : undefined} alt={tr("{{0}}已选图片", state.label)} loading="lazy" decoding="async" draggable={false} onError={() => setFailed(url)} />
      : <span><ImageOff size={26} />{url ? tr("图片暂不可用") : video ? tr("视频参考") : pending ? tr("参考素材待准备") : tr("暂无已选图片")}</span>}</span>
    <strong title={state.label}>{state.label}</strong><small>{status}</small>
  </button>
})
