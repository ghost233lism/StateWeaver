import { tr, useLocale } from '../../ui/locale'
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Image, ImageOff } from 'lucide-react'
import { useStudioImagePreviewUrl, useMediaVisible } from './image-preview'
import { entries, viewLabel, type Asset, type StudioProject, type Selection } from './model'
import { choosePreviewComposition } from './asset-preview-layout'
import './AssetPreview.css'

/** A small cover composition, not a second asset catalog. Exact states and all
 * historical versions remain in the inspector. Never use a rejected fallback. */
export function assetPreviewImages(project: StudioProject, assetId: string, stateId?: string) {
  const states = entries(project.states).filter((state) => state.asset_id === assetId && (!stateId || state.id === stateId))
    .sort((a, b) => Number(b.is_base) - Number(a.is_base) || a.story_time - b.story_time)
  for (const state of states) {
    const seen = new Set<string>()
    const images = Object.entries(state.selected_revisions ?? {}).flatMap(([view, id]) => {
      const reference = project.references?.[id]
      const media = reference?.media_id ? project.media?.[reference.media_id] : undefined
      if (!reference || reference.status !== 'selected' || reference.state_id !== state.id || !media?.mime_type.startsWith('image/')) return []
      const key = media.sha256 || reference.media_id!
      if (seen.has(key)) return []
      seen.add(key)
      return [{ reference: { ...reference, id }, media, state, view }]
    })
    if (images.length) return images
  }
  return []
}

export function AssetPreview({ project, asset, onSelect, stateId, loading = false }: {
  project: StudioProject; asset: Asset & { id: string }; onSelect: (selection: Selection) => void; stateId?: string; loading?: boolean
}) {
  useLocale()

  const images = useMemo(() => assetPreviewImages(project, asset.id, stateId), [project.states, project.references, project.media, asset.id, stateId])
  const frame = useRef<HTMLDivElement | null>(null)
  const { ref: observe, visible } = useMediaVisible()
  const setFrame = useCallback((node: HTMLDivElement | null) => { frame.current = node; observe(node) }, [observe])
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [loadedRatios, setLoadedRatios] = useState<Record<string, number>>({})
  useLayoutEffect(() => {
    const element = frame.current
    if (!element) return
    const measure = () => {
      const width = Math.max(0, element.clientWidth - 16), height = Math.max(0, element.clientHeight - 16)
      setSize((current) => current.width === width && current.height === height ? current : { width, height })
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const composition = useMemo(() => choosePreviewComposition(images.map((item) => loadedRatios[item.reference.id]
    ?? (item.media.width && item.media.height ? item.media.width / item.media.height : 1)), size.width, size.height), [images, loadedRatios, size])
  const tiles = composition?.tiles ?? (images.length ? [{ index: 0, width: undefined, height: undefined }] : [])
  return <div ref={setFrame} className="asset-preview nodrag nopan" role="group" aria-label={tr("{{0}}图片预览", asset.name)}
    onPointerDown={(event) => event.stopPropagation()}>
    {images.length ? <div className={`asset-preview-mosaic${composition ? '' : ' is-measuring'}`} data-preview-count={tiles.length}
      style={composition && { width: composition.width, height: composition.height, flexDirection: composition.direction }}>
      {tiles.map((tile) => {
        const item = images[tile.index]
        return <PreviewImage key={item.reference.id} item={item} assetName={asset.name} width={tile.width} height={tile.height} visible={visible}
          onRatio={(ratio) => setLoadedRatios((current) => current[item.reference.id] === ratio ? current : { ...current, [item.reference.id]: ratio })}
          onOpen={() => onSelect({ kind: 'reference', id: item.reference.id, stateId: item.state.id, referenceId: item.reference.id })} />
      })}
    </div> : <button className="asset-preview-empty" onClick={() => onSelect({ kind: 'asset', id: asset.id, stateId })} aria-label={tr("查看{{0}}素材", asset.name)}><Image size={26} /><span>{loading ? tr("正在加载预览…") : tr("暂无预览")}</span></button>}
  </div>
}

function PreviewImage({ item, assetName, width, height, onOpen, onRatio, visible }: {
  item: ReturnType<typeof assetPreviewImages>[number]; assetName: string; width?: number; height?: number; onOpen: () => void; onRatio: (ratio: number) => void; visible: boolean
}) {
  useLocale()

  const [failed, setFailed] = useState(false)
  const imagePreviewUrl = useStudioImagePreviewUrl()
  const title = `${assetName} · ${item.state.label} · ${viewLabel(item.view)}`
  return <button className="asset-preview-image" title={title} aria-label={tr("查看{{0}}", title)} style={{ width, height }} onClick={onOpen}>
    {failed ? <span className="asset-preview-unavailable"><ImageOff size={22} />{tr("图片暂不可用")}</span> : <img src={visible ? imagePreviewUrl(item.media.url, width && width <= 128 ? 256 : 512) : undefined} alt={title} loading="lazy" decoding="async" draggable={false}
      onLoad={(event) => { const img = event.currentTarget; if (img.naturalHeight) onRatio(img.naturalWidth / img.naturalHeight) }} onError={() => setFailed(true)} />}
  </button>
}
