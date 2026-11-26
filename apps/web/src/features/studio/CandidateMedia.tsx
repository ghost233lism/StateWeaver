import { useLocale } from '../../ui/locale'
import type { StudioProject } from './model'
import { useEntityDetail } from './data'
import { MediaPreview } from './Media'

/** Old/scoped projections can omit a page. Resolve that exact media ID only when displayed. */
export function CandidateMedia({ project, mediaId, title }: { project: StudioProject; mediaId: string; title: string }) {
  useLocale()

  const detail = useEntityDetail(project, mediaId && !project.media?.[mediaId] ? `media:${mediaId}` : undefined)
  return <>{detail.status}{!detail.loading && <MediaPreview key={mediaId} media={detail.project.media?.[mediaId]} title={title} />}</>
}
