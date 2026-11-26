import { tr, useLocale } from '../../ui/locale'
import { useId } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { entries, type MemoryEntry, type StudioProject } from './model'
import { EntityLink, EntityText } from './EntityLinks'
import { LazyDetails } from './data'
import { Markdown } from './Markdown'
import { MemoryStoryEntry } from './MemoryStoryEntry'
import { basisLabels, memorySectionLabels, relatesToClip, storySection } from './StoryMemory'
import { readingExcerpt } from './ReadingText'
import { useMemoryDisclosure } from './memory-disclosure'
import './StoryTimeline.css'

export function StoryTimeline({ project, records, selectedClipId }: { project: StudioProject; records: MemoryEntry[]; selectedClipId?: string }) {
  useLocale()

  const id = useId()
  const disclosure = useMemoryDisclosure(project.id ?? 'new')
  const tab = disclosure.expanded('story-view:rules') ? 'rules' : 'events'
  const setTab = (value: 'events' | 'rules') => disclosure.setExpanded('story-view:rules', value === 'rules')
  const events = entries(project.story_events).sort((a, b) => a.order - b.order || a.id.localeCompare(b.id))
  const selectedClip = selectedClipId ? project.clips?.[selectedClipId] : undefined
  const eventRecords = records.filter((entry) => storySection(entry) === 'events')
  // Group only on recorded event anchors. Unpositioned legacy text is not assigned
  // a fabricated story time, and broad records are not duplicated at every event.
  const anchor = (entry: MemoryEntry) => events.find((event) => entry.scope?.event_ids?.includes(event.id) || entry.scope?.starts_at?.event_id === event.id)?.id
  const unplaced = eventRecords.filter((entry) => !anchor(entry))
  const history = records.filter((entry) => storySection(entry) === 'history')
  const renderRecord = (entry: MemoryEntry, index: number) => <MemoryStoryEntry key={entry.id ?? index} entry={entry} compact related={relatesToClip(entry, selectedClip)} expanded={disclosure.expanded(`entry:${entry.id}`)} onExpandedChange={(open) => disclosure.setExpanded(`entry:${entry.id}`, open)} />
  return <div className="memory-story-browser">
    <div className="memory-story-tabs" role="tablist" aria-label={tr("剧情浏览方式")}>
      {([{ value: 'events', label: tr("事件时间线") }, { value: 'rules', label: tr("全片规则") }] as const).map((item) => <button key={item.value} role="tab" id={`${id}-${item.value}`} aria-selected={tab === item.value} tabIndex={tab === item.value ? 0 : -1} aria-controls={`${id}-panel`} onClick={() => setTab(item.value)} onKeyDown={(event) => {
        if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
          event.preventDefault()
          const next = event.key === 'Home' ? 'events' : event.key === 'End' ? 'rules' : tab === 'events' ? 'rules' : 'events'
          setTab(next); document.getElementById(`${id}-${next}`)?.focus()
        }
      }}>{item.label}</button>)}
    </div>
    <div id={`${id}-panel`} role="tabpanel" aria-labelledby={`${id}-${tab}`}>
      {selectedClip && <p className="studio-note memory-selected-clip">{tr("关联片段：")}{selectedClip.title}</p>}
      {tab === 'events' ? <>
        <p className="memory-timeline-caption">{tr("按故事发生顺序浏览")}</p>
        <ol className="memory-event-timeline" aria-label={tr("剧情事件时间线")}>{events.map((event, index) => {
          const changes = eventRecords.filter((entry) => anchor(entry) === event.id)
          const previewDescription = event.source === 'legacy_evidence' && changes.length ? '' : event.description
          const previewChanges = [...changes].sort((a, b) => {
            const priority = { unknown: 0, provisional: 0, observed: 1, fact: 2, plan: 3 }
            return (priority[a.basis] ?? 3) - (priority[b.basis] ?? 3)
          })
          const open = disclosure.expanded(`timeline:${event.id}`)
          const related = selectedClip?.story_event_ids?.includes(event.id) || changes.some((entry) => relatesToClip(entry, selectedClip))
          return <li key={event.id} data-event-id={event.id} data-related={related || undefined} className={`memory-event${related ? ' is-related' : ''}`}>
            <button className="memory-event-toggle" aria-expanded={open} aria-controls={`${id}-event-${index}`} onClick={() => disclosure.setExpanded(`timeline:${event.id}`, !open)}>
              <span className="memory-event-number">{String(index + 1).padStart(2, '0')}</span>
              <span className="memory-event-title"><strong>{event.title}</strong><span className="memory-event-certainty">{event.certainty === 'unknown' ? tr("待核对") : event.certainty === 'confirmed' ? tr("已确认事件") : tr("事件计划")}{event.story_clock ? ` · ${event.story_clock}` : ''}</span></span>
              {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            </button>
            {!open && <div className="memory-event-preview">
              {previewDescription && <p className="memory-event-description"><EntityText interactive={false}>{`${readingExcerpt(previewDescription, 120, 2)}${readingExcerpt(previewDescription, 120, 2) !== previewDescription.trimEnd() ? '…' : ''}`}</EntityText></p>}
              {previewChanges.slice(0, 2).map((entry, changeIndex) => <div className="memory-event-state" key={entry.id ?? changeIndex} data-memory-id={entry.id} data-related={relatesToClip(entry, selectedClip) || undefined}>
                <span className={`memory-basis memory-basis--${entry.basis}`}>{basisLabels[entry.basis] ?? tr("记录")}</span>
                <p><EntityText interactive={false}>{readingExcerpt(entry.attributes?.length ? entry.attributes.map((attribute) => `${attribute.subject_id}：${attribute.value}`).join('；') : entry.text, 100, 2)}</EntityText></p>
              </div>)}
              {changes.length > 2 && <span className="memory-event-more">{tr("还有")} {changes.length - 2} {tr("条状态记录，展开查看")}</span>}
            </div>}
            {open && <div className="memory-event-body" id={`${id}-event-${index}`}>
              <Markdown>{event.description}</Markdown>
              <p><EntityLink id={event.id}>{tr("事件来源与修改历史")}</EntityLink></p>
              {changes.map(renderRecord)}
            </div>}
          </li>
        })}</ol>
        {!events.length && <p className="studio-empty-line">{tr("尚未建立剧情事件时间线；已有规则和原始记录仍可查看。")}</p>}
        {unplaced.length > 0 && <LazyDetails title={tr("未定位到事件的状态记录（{{0}} 条）", unplaced.length)}><p className="studio-note">{tr("这些记录没有可用的事件锚点，保留原文及其适用范围。")}</p>{unplaced.map(renderRecord)}</LazyDetails>}
      </> : <>
        <p className="memory-timeline-caption">{tr("剧本事实与导演约定")}</p>
        {['screenplay', 'directives'].map((section) => {
          const items = records.filter((entry) => storySection(entry) === section)
          return <section className="studio-memory-section" key={section}><h3>{memorySectionLabels[section]}<span>{items.length}</span></h3>{items.length ? items.map(renderRecord) : <p className="studio-empty-line">{tr("暂无记录")}</p>}</section>
        })}
      </>}
      {history.length > 0 && <LazyDetails title={tr("历史原文（{{0}} 条）", history.length)}>{history.map(renderRecord)}</LazyDetails>}
    </div>
  </div>
}
