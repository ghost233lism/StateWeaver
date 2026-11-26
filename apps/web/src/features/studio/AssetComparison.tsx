import { tr, useLocale } from '../../ui/locale'
import { useRef, useState, type PointerEvent } from 'react'
import { Check, ImageOff, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react'
import { useStudioMediaUrl } from './dataContext'
import { useStudioImagePreviewUrl } from './image-preview'
import { viewLabel, referenceName, type Asset, type AssetState, type Reference, type StudioProject } from './model'
import { useEntityDetail } from './data'
import { Markdown } from './Markdown'
import './MemoryWorkspace.css'

type IdentifiedReference = Reference & { id: string }

export function AssetComparison({ project, asset, state, references, initialReferenceId, ordinals = {} }: {
  project: StudioProject
  asset: Asset & { id: string }
  state: AssetState & { id: string }
  references: IdentifiedReference[]
  initialReferenceId?: string
  ordinals?: Record<string, number>
}) {
  useLocale()

  const scoped = state.asset_id === asset.id ? references.filter((reference) => reference.state_id === state.id) : []
  const views = [...new Set(scoped.map((reference) => reference.view))].filter((view) => scoped.filter((reference) => reference.view === view).length >= 2)
  const initialView = scoped.find((reference) => reference.id === initialReferenceId)?.view
  const [chosenView, setChosenView] = useState(initialView && views.includes(initialView) ? initialView : views[0])
  const view = chosenView ?? views[0]
  const visibleViews = [...new Set([...(view ? [view] : []), ...views])]
  if (!view) return <p className="studio-empty-line">{tr("同一资产、剧情状态和视角至少需要两张参考图才能比较。可继续翻阅修订历史。")}</p>
  return <section className="asset-comparison" aria-label={tr("参考图比较")}>
    <header><h3>{tr("参考图比较")}</h3><label>{tr("视角")}<select value={view} aria-label={tr("比较视角")} onChange={(event) => setChosenView(event.target.value)}>{visibleViews.map((item) => <option key={item} value={item}>{viewLabel(item)}</option>)}</select></label></header>
    <p className="studio-note">{asset.name} / {state.label} / {viewLabel(view)}{tr("。拖动画面同步平移，缩放同时作用于两张图。")}</p>
    <ComparisonPair key={`${asset.id}:${state.id}:${view}`} project={project} state={state} references={scoped.filter((reference) => reference.view === view)} initialReferenceId={initialReferenceId} ordinals={ordinals} />
  </section>
}

function ComparisonPair({ project, state, references, initialReferenceId, ordinals }: {
  project: StudioProject; state: AssetState & { id: string }; references: IdentifiedReference[]; initialReferenceId?: string; ordinals: Record<string, number>
}) {
  useLocale()

  const preferred = references.find((reference) => reference.id === initialReferenceId) ?? references.find((reference) => reference.id === state.selected_revisions?.[reference.view]) ?? references[0]
  // Retain only the chosen pair while the surrounding history page changes.
  const [leftPinned, setLeftPinned] = useState(() => preferred)
  const [rightPinned, setRightPinned] = useState(() => references.find((reference) => reference.id !== preferred.id)!)
  const left = references.find((reference) => reference.id === leftPinned.id) ?? leftPinned
  const right = references.find((reference) => reference.id === rightPinned.id) ?? rightPinned
  const choices = [...references, ...[left, right].filter((reference) => !references.some((item) => item.id === reference.id))]
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const drag = useRef<{ pointerId: number; x: number; y: number; panX: number; panY: number } | null>(null)
  const reset = () => { setZoom(1); setPan({ x: 0, y: 0 }) }
  const changeZoom = (value: number) => setZoom(Math.min(4, Math.max(1, value)))
  const startPan = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    drag.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y }
  }
  const movePan = (event: PointerEvent<HTMLDivElement>) => {
    if (!drag.current || drag.current.pointerId !== event.pointerId) return
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || !rect.height) return
    setPan({ x: drag.current.panX + (event.clientX - drag.current.x) / rect.width * 100, y: drag.current.panY + (event.clientY - drag.current.y) / rect.height * 100 })
  }
  const revisionLabel = (reference: IdentifiedReference) => referenceName(reference, ordinals[reference.id])
  return <>
    <div className="asset-comparison-tools"><button className="studio-icon" aria-label={tr("缩小两张参考图")} disabled={zoom <= 1} onClick={() => changeZoom(zoom - .25)}><ZoomOut size={16} /></button><label>{tr("缩放")}<input aria-label={tr("同步缩放")} type="range" min="1" max="4" step="0.25" value={zoom} onChange={(event) => changeZoom(Number(event.target.value))} /></label><output aria-live="polite">{Math.round(zoom * 100)}%</output><button className="studio-icon" aria-label={tr("放大两张参考图")} disabled={zoom >= 4} onClick={() => changeZoom(zoom + .25)}><ZoomIn size={16} /></button><button className="studio-text-button" onClick={reset}><RotateCcw size={14} />{tr("复位画面")}</button></div>
    <div className="asset-comparison-pair">{([left, right] as const).map((reference, index) => <article key={index} className="asset-comparison-side">
      <label>{index === 0 ? tr("左侧修订") : tr("右侧修订")}<select aria-label={index === 0 ? tr("左侧修订") : tr("右侧修订")} value={reference.id} onChange={(event) => { const chosen = choices.find((item) => item.id === event.target.value)!; if (index === 0) setLeftPinned(chosen); else setRightPinned(chosen); reset() }}>{choices.filter((item) => item.id !== (index === 0 ? right.id : left.id)).map((item) => <option key={item.id} value={item.id}>{revisionLabel(item)}{state.selected_revisions?.[item.view] === item.id ? tr("（已选用）") : ''}</option>)}</select></label>
      <div className="asset-comparison-viewport" role="group" aria-label={tr("{{0}}参考图，可拖动或使用方向键平移", index === 0 ? tr("左侧") : tr("右侧"))} tabIndex={0} onPointerDown={startPan} onPointerMove={movePan} onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }} onLostPointerCapture={() => { drag.current = null }} onKeyDown={(event) => {
        const delta: Record<string, [number, number]> = { ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5] }
        if (delta[event.key]) { event.preventDefault(); const [x, y] = delta[event.key]; setPan((current) => ({ x: current.x + x, y: current.y + y })) }
        if (event.key === 'Home') { event.preventDefault(); reset() }
      }}><ComparisonImage project={project} reference={reference} label={revisionLabel(reference)} original={zoom > 1} transform={`translate(${pan.x}%, ${pan.y}%) scale(${zoom})`} /></div>
      <div className="asset-comparison-caption"><strong>{revisionLabel(reference)}</strong><span className={state.selected_revisions?.[reference.view] === reference.id ? 'is-selected' : ''}>{state.selected_revisions?.[reference.view] === reference.id ? <><Check size={13} />{tr("已选用")}</> : tr("未选用")}</span></div>
      <Markdown className="asset-comparison-reason">{reference.reason || (reference.parent_id ? tr("基于已有参考图修改") : tr("未记录修订原因"))}</Markdown>
    </article>)}</div>
  </>
}

function ComparisonImage({ project, reference, label, transform, original }: { project: StudioProject; reference: IdentifiedReference; label: string; transform: string; original: boolean }) {
  useLocale()

  const detail = useEntityDetail(project, `reference:${reference.id}`)
  const loaded = detail.project.references?.[reference.id] ?? reference
  const media = loaded.media_id ? detail.project.media?.[loaded.media_id] : undefined
  const [failedUrl, setFailedUrl] = useState('')
  const mediaUrl = useStudioMediaUrl()
  const imagePreviewUrl = useStudioImagePreviewUrl()
  const url = media ? original ? mediaUrl(media.url) : imagePreviewUrl(media.url, 1024) : undefined
  return <>{detail.status}{url && media?.mime_type.startsWith('image/') && failedUrl !== url ? <img src={url} alt={label} loading="lazy" draggable={false} style={{ transform }} onError={() => setFailedUrl(url)} /> : <div className="asset-comparison-empty"><ImageOff size={22} /><span>{failedUrl === url ? tr("图片加载失败") : tr("暂无可用图片")}</span>{failedUrl === url && <button className="studio-text-button" onClick={() => setFailedUrl('')}>{tr("重试加载")}</button>}</div>}</>
}
