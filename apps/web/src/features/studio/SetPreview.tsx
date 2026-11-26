import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState } from 'react'
import { Film, LoaderCircle, Maximize2, Play, RefreshCw } from 'lucide-react'
import type { StudioClient } from './client'
import { useStudioMediaUrl } from './dataContext'
import { useStudioData } from './data'
import { PlayableMedia } from './PlayableMedia'
import { setClips, type Selection, type SetPreview as Preview, type StudioProject } from './model'
import './SetPreview.css'

/** One immutable movie per ordered selection; never concatenate rejected alternatives. */
export function SetPreview({ project, setId, compact = false, client: providedClient, onSelect }: {
  project: StudioProject; setId: string; compact?: boolean; client?: StudioClient
  onSelect?: (selection: Selection) => void
}) {
  useLocale()

  const mediaUrl = useStudioMediaUrl()
  const source = useStudioData()
  const client = providedClient ?? source?.client
  const clips = setClips(project, setId)
  const gap = clips.findIndex((clip) => clip.status !== 'accepted' || !clip.accepted_candidate_id)
  const prefix = gap < 0 ? clips : clips.slice(0, gap)
  const signature = JSON.stringify([project.id, setId, clips.map((clip) => [clip.id, clip.status, clip.accepted_candidate_id])])
  const title = tr("片场连播")
  const [enabled, setEnabled] = useState(!compact)
  const [retry, setRetry] = useState(0)
  const [loaded, setLoaded] = useState<{ signature: string; preview: Preview } | null>(null)
  const [displayed, setDisplayed] = useState<Preview | null>(null)
  const [error, setError] = useState<{ signature: string; message: string } | null>(null)
  const playing = useRef(false)
  const screenRef = useRef<HTMLDivElement>(null)
  const hasPrefix = prefix.length > 0
  useEffect(() => {
    if (!enabled || !hasPrefix || !client?.setPreview) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let first = true
    setError(null); setLoaded(null)
    const poll = async () => {
      try {
        const preview = await client.setPreview!(project.id!, setId, { prepare: true, retry: first && retry > 0 }, controller.signal)
        if (controller.signal.aborted) return
        first = false
        setLoaded({ signature, preview })
        if (preview.status === 'ready' && preview.media) {
          setDisplayed((previous) => {
            const stillSelected = previous?.segments?.every((segment, index) => prefix[index]?.id === segment.clip_id && prefix[index]?.accepted_candidate_id === segment.candidate_id)
            // Appending a Clip must not interrupt a movie already playing. Replaced
            // or revoked selections, however, must disappear immediately.
            return previous && stillSelected && playing.current ? previous : preview
          })
        }
        if (preview.status === 'preparing') timer = setTimeout(() => { void poll() }, 1000)
      } catch (reason) {
        if (!controller.signal.aborted) setError({ signature, message: reason instanceof Error ? reason.message : "连播预览加载失败，请重试。" })
      }
    }
    void poll()
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer) }
    // Selection identity controls refresh, independently of transcript/revision updates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, project.id, setId, enabled, signature, hasPrefix, retry])

  const current = loaded?.signature === signature ? loaded.preview : null
  const validDisplay = displayed?.set_id === setId && displayed.segments?.every((segment, index) => prefix[index]?.id === segment.clip_id && prefix[index]?.accepted_candidate_id === segment.candidate_id)
  const movie = validDisplay ? displayed?.media : null
  const update = movie && current?.status === 'ready' && current.fingerprint !== displayed?.fingerprint
  const failure = error?.signature === signature ? error.message : current?.status === 'failed' ? current.message : null
  const preparing = enabled && hasPrefix && Boolean(client?.setPreview) && !failure && (!current || current.status === 'preparing')
  const refresh = () => { setEnabled(true); setError(null); setRetry((value) => value + 1) }
  const retryPlayback = () => { playing.current = false; setDisplayed(null); refresh() }
  const movieUrl = movie ? (providedClient?.mediaUrl ?? mediaUrl)(movie.url) : ''
  // Explicitly retry with a fresh fetch after the server has verified the cache.
  const playbackUrl = retry ? `${movieUrl}${movieUrl.includes('?') ? '&' : '?'}preview_retry=${retry}` : movieUrl
  const waitingTitle = current?.waiting_clip_title ?? (gap >= 0 ? clips[gap].title : null)
  return <section className={`studio-set-preview${compact ? ' is-compact' : ''} nodrag nopan nowheel`} aria-label={`${project.sets?.[setId]?.title ?? ''} · ${title}`}
    onClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
    <header className="studio-set-preview-heading"><div><strong><Film size={16} aria-hidden="true" />{title}</strong>
      <span>{prefix.length} / {clips.length} {tr("Clip 可连播")}</span></div>
      {compact && onSelect && <button className="studio-icon" aria-label={tr("放大{{0}}", title)} title={tr("放大{{0}}", title)} onClick={() => {
        const video = screenRef.current?.querySelector('video')
        if (video && !video.paused) video.pause()
        playing.current = false
        onSelect({ kind: 'set-preview', id: setId })
      }}><Maximize2 size={16} /></button>}
    </header>
    <div ref={screenRef} className="studio-set-preview-screen" onPlayCapture={() => { playing.current = true }} onPauseCapture={() => { playing.current = false }} onEndedCapture={() => { playing.current = false }}>
      {movie ? <PlayableMedia key={movie.id} media={movie} src={playbackUrl} onRetry={retryPlayback} title={tr("{{0}} · 拼接预览", project.sets?.[setId]?.title ?? '')} />
        : <div className="studio-set-preview-placeholder">
          {preparing ? <><LoaderCircle size={25} className="studio-spinner" /><span role="status">{tr("正在准备连播预览…")}</span></>
            : failure ? <><Film size={25} /><span role="alert">{trSystem(failure)}</span><button className="studio-button" onClick={refresh}><RefreshCw size={14} />{tr("重试预览")}</button></>
            : hasPrefix ? <><Film size={27} /><button className="studio-button" onClick={() => enabled ? refresh() : setEnabled(true)} disabled={!client?.setPreview}><Play size={15} />{tr("播放已选 Clip")}</button>{!client?.setPreview && <span>{tr("当前服务暂不支持连播")}</span>}</>
            : <><Film size={27} /><span>{clips.length ? tr("首条 Clip 选用后即可连播") : tr("等待 Clip 规划与选用")}</span></>}
        </div>}
    </div>
    <footer className="studio-set-preview-footer">
      {update ? <button className="studio-text-button" onClick={() => { playing.current = false; setDisplayed(current) }}><RefreshCw size={13} />{tr("新选片已就绪，更新预览")}</button>
        : movie && failure ? <button className="studio-text-button" onClick={refresh}>{tr("新预览加载失败，重试")}</button>
        : movie && preparing ? <span role="status">{tr("正在更新连播…")}</span>
        : current?.status === 'idle' ? <button className="studio-text-button" onClick={refresh}>{current.message || tr("重新准备预览")}</button>
        : <span title={waitingTitle ? tr("等待「{{0}}」选用后接入", waitingTitle) : tr("按当前选用版本，保留原声")}>{waitingTitle ? tr("等待「{{0}}」", waitingTitle) : tr("当前选用版本 · 保留原声")}</span>}
      {movie?.duration != null && <span>{movie.duration.toFixed(1)} {tr("秒")}</span>}
    </footer>
    {!compact && displayed && movie && <ol className="studio-set-preview-segments">{displayed.segments?.map((segment) => <li key={segment.clip_id}>
      <button className="studio-text-button" onClick={() => onSelect?.({ kind: 'clip', id: segment.clip_id, candidateId: segment.candidate_id })}>Clip {String(segment.order + 1).padStart(2, '0')} · {segment.title}</button>
      <span>{segment.duration == null ? '' : tr("{{0}} 秒", segment.duration.toFixed(1))}</span>
    </li>)}</ol>}
  </section>
}
