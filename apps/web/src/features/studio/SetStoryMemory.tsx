import { tr, useLocale } from '../../ui/locale'
import { useState } from 'react'
import { LazyDetails, useEntityDetail } from './data'
import { EntityLink, EntityText } from './EntityLinks'
import { MemoryStoryEntry } from './MemoryStoryEntry'
import { Markdown } from './Markdown'
import { setClips, type Clip, type MemoryEntry, type StudioProject } from './model'
import { selectionKind } from './candidate-details'
import { readingExcerpt } from './ReadingText'
import { useMemoryDisclosure } from './memory-disclosure'

export function ClipStatePlan({ project, clip }: { project: StudioProject; clip: Clip }) {
  useLocale()

  return <div className="memory-clip-state-list">
    {clip.starts_at && <p className="studio-note">{tr("故事位置：")}{project.story_events?.[clip.starts_at.event_id]?.title ?? tr("已规划事件")}{clip.starts_at.position === 'before' ? tr("之前") : tr("之后")}{clip.ends_at ? ` → ${project.story_events?.[clip.ends_at.event_id]?.title ?? tr("结束事件")}${clip.ends_at.position === 'before' ? tr("之前") : tr("之后")}` : ''}{tr("。片长")} {clip.duration} {tr("秒单独记录。")}</p>}
    {clip.state_readiness === 'needs_verification' && <p className="reference-plan-warning">{tr("状态信息待核对；总导演可记录有依据的临时制作决定后继续。")}</p>}
    {clip.asset_plan?.map((item) => <article key={item.asset_id}><strong><EntityLink id={item.asset_id}>{project.assets?.[item.asset_id]?.name}</EntityLink></strong><p>{tr("起始：")}<EntityLink id={item.start_state_id}>{project.states?.[item.start_state_id]?.label}</EntityLink>{item.end_state_id && item.end_state_id !== item.start_state_id && <> {tr("→ 目标：")}<EntityLink id={item.end_state_id}>{project.states?.[item.end_state_id]?.label}</EntityLink></>}</p></article>)}
    {clip.state_notes?.map((text, index) => <p className="studio-note" key={index}>{text}</p>)}
  </div>
}

function SelectedActual({ project, clip, showDetails }: { project: StudioProject; clip: Clip; showDetails: boolean }) {
  useLocale()

  const detail = useEntityDetail(project, clip.accepted_candidate_id ? `candidate:${clip.accepted_candidate_id}` : undefined)
  const candidate = clip.accepted_candidate_id ? detail.project.candidates?.[clip.accepted_candidate_id] : undefined
  if (!clip.accepted_candidate_id) return <p className="studio-empty-line">{tr("尚未选用结果，计划不作为实际观察。")}</p>
  const kind = candidate ? selectionKind(candidate) : undefined
  return <>{detail.status}<p><EntityLink id={clip.accepted_candidate_id}>{tr("查看这一版已选视频")}</EntityLink>{kind === 'system_fallback' ? tr(" · 系统兜底选用") : kind === 'user_selection' ? tr(" · 用户临时选用") : kind === 'director_compromise' ? tr(" · 总导演妥协选用") : ''}</p>
    {showDetails && candidate?.end_state && <><p>{tr("动作：")}{candidate.end_state.action}</p><p>{tr("空间：")}{candidate.end_state.space}</p><p>{tr("机位：")}{candidate.end_state.camera}</p><p>{tr("人物：")}{candidate.end_state.identity}</p></>}
    {showDetails && candidate?.observed_asset_states && Object.entries(candidate.observed_asset_states).map(([id, text]) => <p key={id}><EntityLink id={id} />：{text}</p>)}
    {showDetails && !candidate?.end_state && detail.loaded && <p className="studio-note">{tr("未记录可核实的实际末态，接续前需要核对视频。")}</p>}
  </>
}

function ClipStory({ project, clip, entries, initiallyOpen }: { project: StudioProject; clip: Clip & { id: string }; entries: MemoryEntry[]; initiallyOpen: boolean }) {
  useLocale()

  const disclosure = useMemoryDisclosure(project.id ?? 'new')
  const [opened, setOpened] = useState<boolean>()
  const open = opened ?? (initiallyOpen || disclosure.expanded(`clip-story:${clip.id}`))
  const toggle = (value: boolean) => { setOpened(value); disclosure.setExpanded(`clip-story:${clip.id}`, value) }
  const plans = entries.filter((entry) => entry.section === 'clip_plan')
  const actual = entries.filter((entry) => entry.section === 'clip_actual' && clip.accepted_candidate_id && entry.source_candidate_id === clip.accepted_candidate_id)
  const otherActual = entries.filter((entry) => entry.section === 'clip_actual' && !actual.includes(entry))
  const summary = actual[0]?.text ?? clip.description
  const descriptionRecorded = plans.some((entry) => entry.text.trim() === clip.description.trim())
  return <details className="memory-clip-group" open={open} onToggle={(event) => { if (event.currentTarget.open !== open) toggle(event.currentTarget.open) }}>
    <summary onClick={(event) => { event.preventDefault(); toggle(!open) }}><span className="memory-clip-heading"><strong>{clip.title}</strong><small>{clip.accepted_candidate_id ? tr("已有选用结果") : tr("待完成")} · {clip.duration} {tr("秒")}</small></span>
      {!open && <span className="memory-clip-preview"><span className="memory-basis">{actual.length ? tr("已选实际") : tr("计划")}</span><span><EntityText interactive={false}>{`${readingExcerpt(summary, 120, 2)}${readingExcerpt(summary, 120, 2) !== summary.trimEnd() ? '…' : ''}`}</EntityText></span></span>}
    </summary>
    {open && <div>
      <section className="studio-memory-section"><h3>{tr("计划")}</h3>{!descriptionRecorded && <Markdown>{clip.description}</Markdown>}<ClipStatePlan project={project} clip={clip} />
        {plans.map((entry) => <MemoryStoryEntry key={entry.id} entry={entry} compact />)}
      </section>
      <section className="studio-memory-section"><h3>{tr("已选实际结果")}</h3><SelectedActual project={project} clip={clip} showDetails={!actual.length} />
        {actual.map((entry) => <MemoryStoryEntry key={entry.id} entry={entry} compact />)}
        {otherActual.length > 0 && <LazyDetails title={tr("其他结果的原始观察")}><p className="studio-note">{tr("以下记录未绑定当前选用版本，保留原文与来源供核对。")}</p>{otherActual.map((entry) => <MemoryStoryEntry key={entry.id} entry={entry} compact />)}</LazyDetails>}
      </section>
      <section className="studio-memory-section"><h3>{tr("接续要求")}</h3>
        {Object.entries(clip.continuity ?? {}).map(([key, value]) => <p key={key}>{({ action_direction: tr("动作方向"), speed_trend: tr("速度趋势"), camera_motion: tr("机位运动"), opening_action: tr("开头动作"), ending_action: tr("末尾动作") } as Record<string, string>)[key] ?? tr("接续约定")}：{value}</p>)}
        {entries.filter((e) => e.section === 'clip_continuation').map((e) => <MemoryStoryEntry key={e.id} entry={e} compact />)}
        {!entries.some((e) => e.section === 'clip_continuation') && <p className="studio-note">{tr("同一片场的下一段从当前已选视频的实际末态继续。跨片场变化由总导演发布适用的剧情记忆。")}</p>}
        {clip.reference_plan_id && <p><EntityLink id={clip.reference_plan_id}>{tr("查看本段精确参考清单")}</EntityLink></p>}
      </section>
    </div>}
  </details>
}

export function SetStoryMemory({ project, setId, entries, selectedClipId }: { project: StudioProject; setId: string; entries: MemoryEntry[]; selectedClipId?: string }) {
  useLocale()

  const clips = setClips(project, setId)
  const shared = entries.filter((entry) => !entry.scope?.clip_ids?.length || entry.section === 'set_shared')
  return <div className="memory-set-story"><LazyDetails title={tr("片场共用约定{{0}}", shared.length ? tr("（{{0}} 条）", shared.length) : '')}><p className="studio-note">{tr("片场内保持时空连续，Clip 内允许切镜；相邻 Clip 默认用真实末帧与尾 3 秒接续。")}</p>{shared.map((entry) => <MemoryStoryEntry key={entry.id} entry={entry} compact />)}</LazyDetails>
    {clips.map((clip) => <ClipStory key={clip.id} project={project} clip={clip} entries={entries.filter((entry) => entry.scope?.clip_ids?.includes(clip.id))} initiallyOpen={clip.id === selectedClipId} />)}
    {!clips.length && <p className="studio-empty-line">{tr("导演规划 Clip 后，计划、实际结果和接续要求会按段显示。")}</p>}
  </div>
}
