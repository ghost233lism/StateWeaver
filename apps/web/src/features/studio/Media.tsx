import { tr, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState } from 'react'
import { Download, ImageOff, X } from 'lucide-react'
import { useStudioMediaUrl } from './dataContext'
import type { Media } from './model'
import { PlayableMedia } from './PlayableMedia'
import { useStudioImagePreviewUrl, useMediaVisible } from './image-preview'

export function MediaPreview({ media, title, compact = false }: { media?: Media; title: string; compact?: boolean }) {
  useLocale()

  const mediaUrl = useStudioMediaUrl()
  const imagePreviewUrl = useStudioImagePreviewUrl()
  const [failed, setFailed] = useState('')
  const [open, setOpen] = useState(false)
  const { ref: image, visible } = useMediaVisible()
  if (!media) return <div className="studio-media-empty"><ImageOff size={24} /><span>{tr("暂无可用素材")}</span></div>
  const src = mediaUrl(media.url)
  if (failed === src) return <div className="studio-media-empty"><ImageOff size={24} /><span>{tr("素材加载失败")}</span><button className="studio-button" onClick={() => setFailed('')}>{tr("重试加载")}</button></div>
  if (media.mime_type.startsWith('video/') || media.mime_type.startsWith('audio/')) return <PlayableMedia key={`${media.id}:${src}`} media={media} src={src} title={title} />
  if (!media.mime_type.startsWith('image/')) return <a className="studio-button" href={src} download><Download size={16} />{tr("下载")}{title}</a>
  return <>
    <button ref={image} className={`studio-image-button${compact ? ' is-compact' : ''}`} onClick={() => setOpen(true)} aria-label={tr("放大{{0}}", title)}><img src={visible ? imagePreviewUrl(media.url, compact ? 512 : 1024) : undefined} alt={title} loading="lazy" decoding="async" onError={() => setFailed(src)} /></button>
    {open && <ImageLightbox src={src} title={title} onClose={() => setOpen(false)} />}
  </>
}

function ImageLightbox({ src, title, onClose }: { src: string; title: string; onClose: () => void }) {
  useLocale()

  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close() }, [])
  return <dialog ref={ref} className="studio-lightbox" aria-label={title} onKeyDown={(event) => { if (event.key === 'Escape') event.stopPropagation() }} onCancel={(event) => { event.preventDefault(); event.stopPropagation(); onClose() }} onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <button className="studio-icon" autoFocus aria-label={tr("关闭图片")} onClick={onClose}><X /></button>
    <img src={src} alt={title} />
    <a href={src} download className="studio-button"><Download size={16} />{tr("下载原图")}</a>
  </dialog>
}
