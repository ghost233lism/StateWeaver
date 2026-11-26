import { tr, useLocale } from '../../ui/locale'
import { useState } from 'react'
import { Clapperboard } from 'lucide-react'
import { useStudioImagePreviewUrl } from './image-preview'
import { Status } from './Status'
import type { ProjectSummary } from './model'

export function ProjectCover({ project }: { project: ProjectSummary }) {
  useLocale()

  const imagePreviewUrl = useStudioImagePreviewUrl()
  const src = project.preview_media?.mime_type.startsWith('image/') ? imagePreviewUrl(project.preview_media.url) : ''
  const [failed, setFailed] = useState('')
  return <div className={`studio-project-cover${src && failed !== src ? ' has-preview' : ''}`}>
    {src && failed !== src ? <img src={src} alt={tr("{{0}} 预览", project.title)} loading="lazy" decoding="async" onError={() => setFailed(src)} /> : <Clapperboard size={32} aria-hidden="true" />}
    <Status value={project.status} />
  </div>
}
