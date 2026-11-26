import { tr, useLocale } from '../../ui/locale'
import { BookOpen, Check, Film, LayoutGrid } from 'lucide-react'
import type { StudioClient } from './client'
import { activeClips, orderedSets, unitDisplayTitle, type DirectorMode, type Selection, type StudioProject } from './model'
import { StudioCanvas } from './Canvas'
import { Inspector } from './Inspector'
import { ScreenplayStudio } from './Screenplay'
import { FinalFilm } from './FinalFilm'
import { ProjectOutlineFrame } from './ProjectOutlineFrame'
import { ProductionScope } from './ProductionProgress'
import './InspectorWorkspace.css'

export type StudioPage = 'screenplay' | 'canvas' | 'final'
type Props = {
  client: StudioClient; project: StudioProject; page: StudioPage; selection: Selection | null; expanded: Set<string>; busy: boolean
  onPage: (page: StudioPage) => void; onSelect: (selection: Selection) => void; onSelectResult: (selection: Selection) => void
  onToggle: (id: string) => void; onClose: () => void; onBack?: () => void
  onMessage: (text: string) => Promise<boolean>; onConfirm: (mode?: DirectorMode) => void
  onControl: (actorId: string, action: 'pause' | 'resume') => void
}

export default function StudioWorkspace({ client, project, page, selection, expanded, busy, onPage, onSelect, onSelectResult, onToggle, onClose, onBack, onMessage, onConfirm, onControl }: Props) {
  useLocale()

  return <>
    <nav className="studio-page-tabs" aria-label={tr("制作阶段")}>{([
      ['screenplay', BookOpen, tr("编剧")], ['canvas', LayoutGrid, tr("制作")], ['final', Film, tr("成片")],
    ] as const).map(([id, Icon, name]) => <button key={id} className={page === id ? 'is-active' : ''} onClick={() => onPage(id)} aria-current={page === id ? 'page' : undefined}><Icon size={17} />{name}{id === 'screenplay' && project.screenplay?.confirmed_at && <Check size={13} />}</button>)}</nav>
    <ProductionScope key={project.id} client={client} project={project} onSelect={onSelect}>
    <div className="studio-workspace">
      {page === 'canvas' && <ProjectOutline project={project} selection={selection} onSelect={onSelect} />}
      <main className={`studio-main studio-main--${page}`}>
        {page === 'canvas' ? <StudioCanvas client={client} project={project} selection={selection} expanded={expanded} onSelect={onSelect} onToggle={onToggle} /> : page === 'screenplay' ? <ScreenplayStudio project={project} busy={busy} onMessage={onMessage} onConfirm={onConfirm} /> : <FinalFilm project={project} onSelect={onSelect} />}
      </main>
      {selection && <Inspector key={`${selection.kind}:${selection.id}:${selection.candidateId ?? ''}:${selection.referenceId ?? ''}:${selection.stateId ?? ''}`} project={project} selection={selection} busy={busy} onClose={onClose} onSelect={onSelectResult} onBack={onBack} onControl={onControl} onScreenplay={() => onPage('screenplay')} />}
    </div>
    </ProductionScope>
  </>
}

function ProjectOutline({ project, selection, onSelect }: { project: StudioProject; selection: Selection | null; onSelect: (selection: Selection) => void }) {
  useLocale()

  const clips = activeClips(project)
  const accepted = clips.filter((clip) => clip.status === 'accepted').length
  return <ProjectOutlineFrame><div className="studio-outline-progress"><span>{tr("制作进度")}</span><strong>{accepted}<small> / {clips.length} {tr("已选用")}</small></strong><progress value={accepted} max={clips.length || 1} aria-label={tr("已选用片段进度")} /></div>
    <button className={selection?.id === 'global' ? 'is-selected' : ''} onClick={() => onSelect({ kind: 'memory', id: 'global' })}><BookOpen size={16} /><span>{tr("全局记忆")}</span><small>{Object.keys(project.assets ?? {}).length} {tr("项资产")}</small></button>
    <h2>{tr("片场")}</h2>{orderedSets(project).map((set) => <button className={selection?.id === set.actor_id ? 'is-selected' : ''} key={set.id} onClick={() => onSelect({ kind: 'actor', id: set.actor_id })}><span className="studio-set-number">{String(set.order + 1).padStart(2, '0')}</span><span title={set.title}>{unitDisplayTitle(set)}</span><i className={`studio-outline-dot studio-outline-dot--${set.status}`} /></button>)}
    {!Object.keys(project.sets ?? {}).length && <p>{tr("总导演准备好后，片场会列在这里。")}</p>}
    {(project.issues?.length ?? 0) > 0 && <details className="studio-project-issues"><summary>{project.issues?.length} {tr("个待处理问题")}</summary><ul>{project.issues?.map((issue, index) => <li key={index}>{issue}</li>)}</ul></details>}
  </ProjectOutlineFrame>
}
