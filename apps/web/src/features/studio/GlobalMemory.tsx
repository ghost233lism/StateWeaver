import { tr, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { ArrowDown, ArrowUp, BookOpen, Box, ChevronDown, ChevronUp, GripVertical, Search, X } from 'lucide-react'
import { useEntityDetail } from './data'
import { storySections, memorySectionLabels, storySection, useMemoryEntries } from './StoryMemory'
import { entries, type Asset, type Selection, type StudioProject } from './model'
import { AssetPreview } from './AssetPreview'
import { EntityLinkProvider } from './EntityLinks'
import { useMemoryDisclosure } from './memory-disclosure'
import { useMemoryStoryResize } from './memory-story-resize'
import { useMemoryCategories } from './memory-fill'
import { StoryTimeline } from './StoryTimeline'
import './MemoryWorkspace.css'

export const assetKindLabels: Record<string, string> = { get character() { return tr("人物") }, get person() { return tr("人物") }, get object() { return tr("物体") }, get prop() { return tr("道具") }, get scene() { return tr("场景") }, get location() { return tr("场景") }, get other() { return tr("其他素材") } }

export { MemoryStoryEntry, firstMemoryParagraph } from './MemoryStoryEntry'

type CardAsset = Asset & { id: string }

/** Saved IDs are a presentation order only; missing/new cards keep catalog order. */
export function orderedAssetGroups(assets: CardAsset[], assetOrder: Record<string, string[]>) {
  const groups = new Map<string, CardAsset[]>()
  for (const asset of assets) groups.set(asset.kind, [...(groups.get(asset.kind) ?? []), asset])
  return Array.from(groups, ([kind, cards]) => {
    const byId = new Map(cards.map((asset) => [asset.id, asset]))
    const ids = [...new Set([...(assetOrder[kind] ?? []), ...cards.map((asset) => asset.id)])]
    return { kind, assets: ids.flatMap((id) => byId.has(id) ? [byId.get(id)!] : []) }
  })
}

type GlobalMemoryProps = {
  project: StudioProject
  onSelect: (selection: Selection) => void
  assetOrder: Record<string, string[]>
  onAssetOrderChange: (kind: string, ids: string[]) => void
  selectedClipId?: string
  storyWidth?: number | null
  onStoryWidthChange?: (width: number | null) => void
  fillHeight?: boolean
}

export function GlobalMemory(props: GlobalMemoryProps) {
  useLocale()

  // A project switch resets transient searches and reads only that project's preferences.
  return <ProjectMemory key={props.project.id ?? 'new'} {...props} />
}

const storyKindLabels = memorySectionLabels

function ProjectMemory({ project: initialProject, onSelect, assetOrder, onAssetOrderChange, storyWidth, onStoryWidthChange, selectedClipId, fillHeight = false }: GlobalMemoryProps) {
  useLocale()

  const detail = useEntityDetail(initialProject, 'memory:global')
  const project = detail.project
  const history = useMemoryEntries(project, 'global')
  const story = history.entries
  const storyKinds = storySections(story)
  const [search, setSearch] = useState('')
  const [kindFilter, setKindFilter] = useState('')
  const [dragging, setDragging] = useState<{ id: string; kind: string } | null>(null)
  const storyResize = useMemoryStoryResize(storyWidth, onStoryWidthChange)
  const visualRegion = useRef<HTMLElement>(null)
  const categoriesRegion = useRef<HTMLDivElement>(null)
  const [storyMaxHeight, setStoryMaxHeight] = useState(520)
  useEffect(() => {
    const visual = visualRegion.current
    if (!visual) return
    const measure = (height: number) => {
      if (!Number.isFinite(height) || height <= 0) return
      const next = Math.max(260, Math.ceil(height))
      setStoryMaxHeight((current) => current === next ? current : next)
    }
    measure(visual.offsetHeight)
    if (typeof ResizeObserver === 'undefined') return
    // Only the visual region drives this cap. Story expansion cannot feed back
    // into the measurement, enlarge its column, or persist a new canvas size.
    const observer = new ResizeObserver(([entry]) => measure(entry?.borderBoxSize?.[0]?.blockSize ?? entry?.contentRect?.height ?? visual.offsetHeight))
    observer.observe(visual)
    return () => observer.disconnect()
  }, [])
  const disclosure = useMemoryDisclosure(initialProject.id ?? 'new')
  const groups = orderedAssetGroups(entries(project.assets), assetOrder)
  const query = search.trim().toLocaleLowerCase()
  const filtering = Boolean(query || kindFilter)
  const assetCount = groups.reduce((count, group) => count + group.assets.length, 0)
  const storyOpen = disclosure.expanded('story', true)
  const visualOpen = filtering || disclosure.expanded('visual', true)
  const visible = groups.map((group) => ({ ...group, visible: group.assets.filter((asset) => (!kindFilter || kindFilter === asset.kind) && `${asset.name} ${asset.kind} ${assetKindLabels[asset.kind] ?? ''}`.toLocaleLowerCase().includes(query)) }))
  const visibleCount = visible.reduce((count, group) => count + group.visible.length, 0)
  const fillKey = JSON.stringify([visualOpen, filtering, visible.map(({ kind, visible: cards }) => [kind, cards.length, disclosure.expanded(`visual:${kind}`, true)])])
  const categoryLayout = useMemoryCategories(categoriesRegion, fillHeight, fillKey)
  const reorder = (kind: string, sourceId: string, targetId: string) => {
    const ids = groups.find((group) => group.kind === kind)?.assets.map((asset) => asset.id) ?? []
    const from = ids.indexOf(sourceId); const to = ids.indexOf(targetId)
    if (from < 0 || to < 0 || from === to) return
    ids.splice(from, 1); ids.splice(to, 0, sourceId)
    onAssetOrderChange(kind, ids)
  }
  return <EntityLinkProvider project={project} onSelect={onSelect}><div className={`global-memory nodrag${fillHeight ? ' global-memory--fill' : ''}`} aria-label={tr("全局记忆内容")}>
    {detail.status}
    <div ref={storyResize.regions} className="global-memory-regions" style={{ '--global-memory-story-width': storyResize.width != null ? `${storyResize.width}px` : undefined } as CSSProperties}>
      <section ref={storyResize.story} id={storyResize.storyId} className="global-memory-story" aria-label={tr("剧情记忆")} style={{ '--global-memory-story-max-height': `${storyMaxHeight}px` } as CSSProperties}>
        <header className="global-memory-region-heading"><h3><BookOpen size={17} />{tr("剧情记忆")}</h3><div className="global-memory-region-actions"><span>{tr("修订")} {project.global_memory?.revision ?? 0}</span><button className="studio-text-button memory-region-toggle" aria-label={tr("{{0}}剧情记忆", storyOpen ? tr("收起") : tr("展开"))} aria-expanded={storyOpen} onClick={() => disclosure.setExpanded('story', !storyOpen)}>{storyOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}{storyOpen ? tr("收起") : tr("展开")}</button></div></header>
        {!storyOpen && <p className="global-memory-folded-summary">{storyKinds.map((kind) => <span key={kind}>{storyKindLabels[kind]} {story.filter((entry) => storySection(entry) === kind).length}</span>)}</p>}
        {storyOpen && <div className="global-memory-story-scroll nowheel" role="region" aria-label={tr("剧情记录")} tabIndex={0} onKeyDown={(event) => {
          if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', ' '].includes(event.key)) event.stopPropagation()
        }}>{history.switcher}<StoryTimeline project={project} records={story} selectedClipId={selectedClipId} />{history.controls}</div>}
        {storyResize.control}
      </section>
      <section ref={visualRegion} className="global-memory-visual" aria-label={tr("视觉资产")} style={{ '--global-memory-asset-height': `${categoryLayout.cardHeight}px` } as CSSProperties}>
        <header className="global-memory-region-heading"><h3><Box size={17} />{tr("视觉资产")}</h3><div className="global-memory-region-actions"><span>{assetCount} {tr("个资产")}</span><button className="studio-text-button memory-region-toggle" aria-label={tr("{{0}}视觉资产", visualOpen ? tr("收起") : tr("展开"))} aria-expanded={visualOpen} disabled={filtering} title={filtering ? tr("清除筛选后可收起") : undefined} onClick={() => disclosure.setExpanded('visual', !visualOpen)}>{visualOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}{visualOpen ? tr("收起") : tr("展开")}</button></div></header>
        <div className="global-memory-filters">
          <label className="global-memory-search"><Search size={15} /><input type="search" aria-label={tr("搜索资产名称或类型")} placeholder={tr("搜索名称或类型")} value={search} onChange={(event) => setSearch(event.target.value)} /></label>
          <select aria-label={tr("资产类型")} value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}><option value="">{tr("全部类型")}</option>{groups.map(({ kind }) => <option key={kind} value={kind}>{assetKindLabels[kind] ?? kind}</option>)}</select>
          {(search || kindFilter) && <button className="studio-text-button" onClick={() => { setSearch(''); setKindFilter('') }}><X size={14} />{tr("清除筛选")}</button>}
        </div>
        {!visualOpen && <p className="global-memory-folded-summary">{groups.map(({ kind, assets }) => <span key={kind}>{assetKindLabels[kind] ?? kind} {assets.length}</span>)}</p>}
        {visualOpen && <><p className="global-memory-order-note">{tr("拖动底部手柄可调整同类资产顺序。")}</p>
        <div ref={categoriesRegion} className="global-memory-categories nowheel">
        {visible.map(({ kind, assets, visible: cards }) => {
          const expanded = filtering || disclosure.expanded(`visual:${kind}`, true)
          const shown = expanded ? cards : cards.slice(0, 3)
          return cards.length > 0 && <section className="global-memory-asset-group" key={kind} aria-label={tr("{{0}}资产", assetKindLabels[kind] ?? kind)} data-asset-kind={kind}>
          <h4>{assetKindLabels[kind] ?? kind}<span>{cards.length}{cards.length !== assets.length ? ` / ${assets.length}` : ''}</span></h4>
          <div className="global-memory-category-viewport nowheel" role="region" aria-label={tr("{{0}}卡片列表", assetKindLabels[kind] ?? kind)} tabIndex={0}
            style={{ height: categoryLayout.heights[kind] }} onKeyDown={(event) => {
              if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', ' '].includes(event.key)) event.stopPropagation()
            }}>
          <div className="global-memory-asset-grid">{shown.map((asset) => {
            const index = assets.findIndex((item) => item.id === asset.id)
            return <article key={asset.id} data-asset-id={asset.id} className={`global-memory-asset-card${dragging?.id === asset.id ? ' is-dragging' : ''}`} onDragStart={(event) => { event.dataTransfer.setData('text/plain', asset.id); event.dataTransfer.effectAllowed = 'move'; setDragging({ id: asset.id, kind }) }} onDragEnd={() => setDragging(null)} onDragOver={(event) => { if (dragging?.kind === kind) { event.preventDefault(); event.dataTransfer.dropEffect = 'move' } }} onDrop={(event) => { event.preventDefault(); if (dragging?.kind === kind) reorder(kind, dragging.id, asset.id); setDragging(null) }}>
              <AssetPreview loading={detail.loading} project={project} asset={asset} onSelect={onSelect} />
              <button className="global-memory-asset-open" onClick={() => onSelect({ kind: 'asset', id: asset.id })} aria-label={tr("查看资产{{0}}", asset.name)}>
                <strong>{asset.name}</strong><small>{tr("剧情状态与版本")}</small>
              </button>
              <div className="global-memory-card-order" draggable title={tr("拖动调整{{0}}的位置", asset.name)}><GripVertical size={14} aria-hidden="true" /><button aria-label={tr("前移{{0}}", asset.name)} disabled={index === 0} onClick={() => reorder(kind, asset.id, assets[index - 1].id)}><ArrowUp size={13} /></button><button aria-label={tr("后移{{0}}", asset.name)} disabled={index === assets.length - 1} onClick={() => reorder(kind, asset.id, assets[index + 1].id)}><ArrowDown size={13} /></button></div>
            </article>
          })}</div></div>
          {!filtering && cards.length > 3 && <button className="studio-text-button memory-group-toggle" aria-label={expanded ? tr("收起{{0}}资产", assetKindLabels[kind] ?? kind) : tr("展开{{0}}全部 {{1}} 个资产", assetKindLabels[kind] ?? kind, cards.length)} aria-expanded={expanded} onClick={() => disclosure.setExpanded(`visual:${kind}`, !expanded)}>{expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}{expanded ? tr("收起更多") : tr("还有 {{0}} 个，展开全部", cards.length - 3)}</button>}
        </section>})}
        {visibleCount === 0 && <p className="studio-empty-line">{groups.length ? tr("没有匹配的资产，清除筛选可恢复全部卡片。") : tr("总导演会在准备阶段建立视觉资产。")}</p>}
        </div></>}
      </section>
    </div>
  </div></EntityLinkProvider>
}
