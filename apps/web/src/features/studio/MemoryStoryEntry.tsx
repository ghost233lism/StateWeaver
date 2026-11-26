import { tr, useLocale } from '../../ui/locale'
import { useState } from 'react'
import { Markdown } from './Markdown'
import { entityExcerpt } from './entity-link-model'
import { MemoryEntryBadges, MemoryEntryMetadata } from './StoryMemory'
import type { MemoryEntry } from './model'

/** Preserve source text, including heading prefixes, through the first paragraph. */
export function firstMemoryParagraph(text: string) {
  const blocks = [...text.matchAll(/\S[\s\S]*?(?=\r?\n\s*\r?\n|$)/g)]
  const first = blocks.find((block) => !/^#{1,6}\s+[^\n]+$/.test(block[0])) ?? blocks[0]
  return first ? text.slice(0, first.index! + first[0].length) : text
}

export function MemoryStoryEntry({ entry, compact = false, expanded: controlledExpanded, onExpandedChange, related = false }: {
  related?: boolean; entry: MemoryEntry; compact?: boolean; expanded?: boolean; onExpandedChange?: (expanded: boolean) => void
}) {
  useLocale()

  const [localExpanded, setLocalExpanded] = useState(false)
  const expanded = controlledExpanded ?? localExpanded
  const paragraph = firstMemoryParagraph(entry.text)
  const preview = compact && paragraph.length > 180 ? `${entityExcerpt(paragraph, 180).trimEnd()}…` : paragraph
  const expandable = compact || preview.trimEnd() !== entry.text.trimEnd()
  return <article className={`studio-memory-entry studio-memory-entry--${entry.kind}${related ? ' is-related' : ''}`} data-memory-id={entry.id} data-related={related || undefined}>
    <Markdown className={compact && !expanded ? 'memory-story-preview' : undefined}>{expanded ? entry.text : preview}</Markdown>
    {expandable && <button className="studio-text-button memory-story-expand" aria-expanded={expanded} onClick={() => { setLocalExpanded(!expanded); onExpandedChange?.(!expanded) }}>{expanded ? tr("收起原文") : tr("展开全文")}</button>}
    {compact && !expanded ? <MemoryEntryBadges entry={entry} /> : <MemoryEntryMetadata entry={entry} />}
  </article>
}
