import { tr, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState, type SyntheticEvent } from 'react'
import { ImageOff, LoaderCircle } from 'lucide-react'
import type { Media } from './model'

const stallTimeout = 15_000
/** Recover only media reads. The same source/version and playback settings stay
 * pinned; retries never submit generation and stop after two failed attempts. */
export function PlayableMedia({ media, src, title, onRetry }: { media: Media; src: string; title: string; onRetry?: () => void }) {
  useLocale()

  const video = media.mime_type.startsWith('video/')
  const element = useRef<HTMLMediaElement | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout>>()
  const watchdog = useRef<ReturnType<typeof setTimeout>>()
  const attempts = useRef(0)
  const playing = useRef(false)
  const reloading = useRef(false)
  const goodSince = useRef<number>()
  const lastPlaybackTime = useRef<number>()
  const resume = useRef<{ time: number; volume: number; muted: boolean; rate: number } | null>(null)
  const [state, setState] = useState<'ready' | 'retrying' | 'failed'>('ready')
  const clearRetry = () => { if (timer.current !== undefined) { clearTimeout(timer.current); timer.current = undefined } }
  const clearWatchdog = () => { if (watchdog.current !== undefined) { clearTimeout(watchdog.current); watchdog.current = undefined } }
  useEffect(() => () => { clearRetry(); clearWatchdog() }, [])
  const remember = (current: HTMLMediaElement) => {
    resume.current ??= { time: Number.isFinite(current.currentTime) ? current.currentTime : 0, volume: current.volume, muted: current.muted, rate: current.playbackRate }
  }
  const reload = () => {
    clearRetry(); clearWatchdog()
    reloading.current = true
    setState('retrying')
    element.current?.load()
    watch()
  }
  const recover = (current: HTMLMediaElement) => {
    if (timer.current !== undefined) return
    clearWatchdog(); remember(current); goodSince.current = undefined
    // A decoder failure is not a stalled connection. Unsupported/transient
    // sources may retry, but even repeated metadata successes cannot reset quota.
    if (!video || current.error?.code === 3 || attempts.current >= 2) { setState('failed'); return }
    const delay = [600, 1800][attempts.current++]
    setState('retrying')
    timer.current = setTimeout(reload, delay)
  }
  const watch = (progress = false) => {
    if (progress) clearWatchdog()
    const current = element.current
    if (!current || current.ended || current.closest('[hidden]') || document.hidden || (current.readyState >= 1 && !playing.current)) { clearWatchdog(); return }
    if (watchdog.current !== undefined) return
    watchdog.current = setTimeout(() => {
      watchdog.current = undefined
      if (!document.hidden && !current.closest('[hidden]')) recover(current)
    }, stallTimeout)
  }
  const onLoadedMetadata = (event: SyntheticEvent<HTMLMediaElement>) => {
    const current = event.currentTarget, saved = resume.current
    if (saved) {
      current.volume = saved.volume; current.muted = saved.muted; current.playbackRate = saved.rate
      if (saved.time > 0) current.currentTime = Number.isFinite(current.duration) ? Math.min(saved.time, current.duration) : saved.time
    }
    reloading.current = false
    setState('ready'); clearWatchdog()
    if (playing.current && !current.closest('[hidden]') && !document.hidden) { void current.play().catch(() => { playing.current = false }); watch() }
  }
  const props = {
    src, controls: true, preload: 'metadata', 'aria-label': title,
    onError: (event: SyntheticEvent<HTMLMediaElement>) => recover(event.currentTarget), onLoadedMetadata,
    onLoadStart: () => { lastPlaybackTime.current = undefined; watch() }, onWaiting: () => watch(), onStalled: () => watch(), onSeeking: () => { goodSince.current = undefined; lastPlaybackTime.current = element.current?.currentTime; watch(true) },
    onCanPlay: () => { resume.current = null; reloading.current = false; setState('ready'); watch() },
    onTimeUpdate: (event: SyntheticEvent<HTMLMediaElement>) => {
      const current = event.currentTarget
      if (!playing.current || current.seeking || current.readyState < 2) return
      if (lastPlaybackTime.current === current.currentTime) return
      lastPlaybackTime.current = current.currentTime
      if (goodSince.current === undefined || current.currentTime < goodSince.current) goodSince.current = current.currentTime
      if (current.currentTime - goodSince.current >= 5) attempts.current = 0
      resume.current = null; reloading.current = false; setState('ready'); watch(true)
    },
    onPlay: () => { playing.current = true; lastPlaybackTime.current = element.current?.currentTime; watch() },
    onEnded: () => { playing.current = false; clearWatchdog() },
    onPause: (event: SyntheticEvent<HTMLMediaElement>) => { if ((!reloading.current && !event.currentTarget.error) || event.currentTarget.closest('[hidden]')) { playing.current = false; clearWatchdog() } },
  }
  return <div className="studio-playable-media" data-kind={video ? 'video' : 'audio'} style={video ? { aspectRatio: `${media.width || 16} / ${media.height || 9}` } : undefined}>
    {video ? <video ref={(node) => { element.current = node }} className="studio-video" {...props} playsInline /> : <audio ref={(node) => { element.current = node }} className="studio-audio" {...props} />}
    {state !== 'ready' && <div className="studio-media-recovery" role={state === 'failed' ? 'alert' : 'status'}>
      {state === 'retrying' ? <><LoaderCircle size={22} className="studio-spinner" /><span>{tr("正在重新加载…")}</span></> : <><ImageOff size={24} /><span>{tr("素材加载失败")}</span><button className="studio-button" onClick={onRetry ?? (() => { attempts.current = 0; reload() })}>{tr("重试加载")}</button></>}
    </div>}
  </div>
}
