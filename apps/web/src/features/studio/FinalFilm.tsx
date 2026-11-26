import { tr, useLocale } from '../../ui/locale'
import { Download, Film, LoaderCircle } from 'lucide-react'
import { useStudioMediaUrl } from './dataContext'
import { activeClips, orderedSets, setClips, type Selection, type StudioProject } from './model'
import { MediaPreview } from './Media'
import { Status } from './Status'
import { DetailValue } from './Transcript'
import { useEntityDetail } from './data'

function deliverySummary(manifest: StudioProject['export_manifest']): string {
  const streams = manifest?.audio_streams
  if (streams === 0) return tr("全片已合成，无音轨。")
  if (typeof streams !== 'number' || streams <= 0) return tr("全片已合成。")
  const segments = manifest?.segments
  if (Array.isArray(segments) && segments.length > 0) {
    const delivery = segments.map((item) => item && typeof item === 'object' ? item.audio_delivery : undefined)
    const silent = delivery.filter((value) => value === 'silent_fill').length
    if (silent === delivery.length) return tr("全片已合成，音轨为静音填充。")
    if (silent > 0) return tr("全片已合成，部分片段的音轨为静音填充。")
    if (delivery.every((value) => value === 'source')) return tr("全片已合成，原生音频随视频保留。")
  }
  return tr("全片已合成，含音轨。")
}

export function FinalFilm({ project: initialProject, onSelect }: { project: StudioProject; onSelect: (selection: Selection) => void }) {
  useLocale()

  const mediaUrl = useStudioMediaUrl()
  const detail = useEntityDetail(initialProject, 'final')
  const project = detail.project
  const media = project.final_media_id ? project.media?.[project.final_media_id] : undefined
  const clips = activeClips(project)
  const accepted = clips.filter((clip) => clip.status === 'accepted').length
  return <section className="studio-final"><header><div><h1>{tr("成片")}</h1><p>{media ? deliverySummary(project.export_manifest) : tr("{{0}} / {{1}} 个片段已选用，完成后由总导演合成。", accepted, clips.length)}</p></div>{media && <a className="studio-button studio-button--primary" href={mediaUrl(media.url)} download><Download size={17} />{tr("下载成片")}</a>}</header>
    <div className="studio-final-player">{media ? <MediaPreview media={media} title={tr("{{0}} 成片", project.title)} /> : detail.error ? <div className="studio-empty">{detail.status}</div> : detail.loading || project.final_media_id ? <div className="studio-empty" role="status"><LoaderCircle size={32} className="studio-spinner" /><p>{tr("正在加载成片…")}</p></div> : <div className="studio-empty"><Film size={44} /><h2>{tr("等待全片完成")}</h2><p>{tr("各片场的制作进度和已生成片段可以在下方查看。")}</p></div>}</div>
    {media && detail.error && detail.status}
    <section className="studio-final-sequence"><h2>{tr("片场顺序")}</h2>{orderedSets(project).map((set) => <article key={set.id}><div><span>Set {String(set.order + 1).padStart(2, '0')}</span><h3>{set.title}</h3><Status value={set.status} /></div><div>{setClips(project, set.id).map((clip) => <button key={clip.id} onClick={() => onSelect({ kind: 'clip', id: clip.id })}><strong>{clip.title}</strong><span>{clip.duration} {tr("秒")}</span><Status value={clip.status} /></button>)}</div></article>)}</section>
    {Object.keys(project.export_manifest ?? {}).length > 0 && <details className="studio-record"><summary>{tr("成片来源记录")}</summary><DetailValue value={project.export_manifest} /></details>}
  </section>
}
