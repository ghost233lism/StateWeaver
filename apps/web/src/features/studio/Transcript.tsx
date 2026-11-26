import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowDown, BookOpen, Box, Check, ChevronDown, CircleAlert, Terminal } from 'lucide-react'
import { transcriptFor, type Selection, type StudioProject, type TranscriptItem, type TranscriptPage } from './model'
import { BodyReader, LazyDetails, useStudioData } from './data'
import { mergeTranscript } from './cache'
import { DetailValue } from './DetailValue'
import { Markdown } from './Markdown'
import { activityContext, activityFor, activityRows, activityTarget, isOperation } from './activity'
import { operationChangeFor } from './operation-change-model'
import { OperationChanges } from './OperationChanges'
import { EntityLinkProvider } from './EntityLinks'
import { ReadingText } from './ReadingText'
import './Transcript.css'

function RawOperation({ item }: { item: TranscriptItem }) {
  useLocale()

  return <div className="studio-operation-body">{item.body_ref ? <BodyReader bodyRef={item.body_ref} /> : <>{item.text && <Markdown>{item.text}</Markdown>}<DetailValue value={item.data} /></>}</div>
}

function hasAdditionalOutput(item: TranscriptItem) {
  if (!item.body_ref) return false
  // The server counts Unicode code points; JS string.length counts UTF-16 units.
  // Unknown historical lengths keep their lazy archive entrypoint.
  return item.text_length == null || Array.from(item.text ?? '').length < item.text_length
}

function safeModelLabel(value: unknown) {
  return typeof value === 'string' && /^[a-zA-Z0-9._:/-]{1,128}$/.test(value) ? value : tr("模型")
}

function modelFailureLabel(value: unknown) {
  switch (value) {
    case 'ProviderCapacityWait': return tr("服务容量暂不可用")
    case 'ProviderContractError': return tr("模型响应不可用")
    default: return tr("模型调用失败")
  }
}

function RoleRuntimeNotice({ item }: { item: TranscriptItem }) {
  useLocale()

  if (item.data?.runtime !== 'agentvideo-v2') return null
  const attempts = item.data.attempts ?? item.data.failures
  const failures = Array.isArray(attempts) ? attempts.slice(0, 20).flatMap((attempt: unknown) => {
    if (!attempt || typeof attempt !== 'object' || !('model' in attempt) || !('error_type' in attempt)) return []
    return [{ model: safeModelLabel(attempt.model), reason: modelFailureLabel(attempt.error_type) }]
  }) : []
  const partial = item.kind === 'message' && item.status === 'failed'
  if (!partial && !failures.length) return null
  return <div className="studio-role-notice">
    {partial && <p className="studio-role-partial" role="note">
      {item.data.preview_interrupted === true ? tr("模型输出中断") : tr("模型尝试失败")} · {safeModelLabel(item.data.model)} {tr("的未完成输出")}
    </p>}
    {failures.length > 0 && <LazyDetails title={tr("同服务重试记录")}>
      <ol>{failures.map(({ model, reason }, index) => <li key={index}>{model} · {reason}</li>)}</ol>
      {item.kind !== 'error' && !partial && <p className="studio-note">{tr("已由")} {safeModelLabel(item.data.model)} {tr("返回结果。")}</p>}
    </LazyDetails>}
  </div>
}

function Operation({ item, project, onSelect, native = [], compact = false }: { item: TranscriptItem; project: StudioProject; onSelect?: (selection: Selection) => void; native?: TranscriptItem[]; compact?: boolean }) {
  useLocale()

  const activity = activityFor(item)
  const icon = item.kind === 'memory_change' ? <BookOpen size={16} /> : item.kind === 'asset_change' ? <Box size={16} /> : <Terminal size={16} />
  const status = activity.status === 'complete' ? <Check size={14} className="studio-operation-check" aria-label={tr("已完成")} /> : <span className={`studio-operation-state ${activity.status === 'failed' ? 'is-failed' : ''}`}>{activity.status === 'running' ? tr("进行中") : activity.status === 'failed' ? tr("失败") : activity.status}</span>
  if (activity.read) return compact
    ? <div className="studio-read-operation"><div className="studio-read-summary">{icon}<strong>{activity.title}</strong>{status}</div></div>
    : <LazyDetails className="studio-read-operation" title={<>{icon}<strong>{activity.title}</strong>{status}<ChevronDown size={14} /></>}><RawOperation item={item} />{native.map((record) => <LazyDetails key={record.id} title={tr("工具调用详情")}><RawOperation item={record} /></LazyDetails>)}</LazyDetails>
  const target = activityTarget(item, project)
  const context = activityContext(item, project)
  const change = compact ? operationChangeFor(item, project) : null
  const targetLabel = target.kind === 'reference-plan' ? tr("查看参考清单") : target.kind === 'reference' ? tr("查看这版参考图") : target.kind === 'clip' ? tr("查看这次生成") : target.kind === 'asset' ? tr("查看资产状态") : target.kind === 'media' ? tr("查看这份素材") : tr("查看操作记录")
  return <section className={`studio-activity studio-activity--${item.kind}${compact ? ' studio-activity--compact' : ''} ${activity.status === 'failed' ? 'studio-activity--failed' : ''}`}>
    <div className="studio-activity-heading">{icon}<strong>{activity.title}</strong>{status}</div>
    {change ? <>{context && change.action !== 'write_memory' && !change.reference && <p className="studio-activity-context">{context}</p>}
      {activity.description && activity.description !== change.summary && <ReadingText className="studio-activity-description" text={activity.description} label={tr("操作说明")} />}
      <OperationChanges change={change} project={project} onSelect={onSelect} /></> : <>{context && <p className="studio-activity-context">{context}</p>}
      {activity.description && <ReadingText className="studio-activity-description" text={activity.description} label={tr("操作说明")} />}</>}
    {activity.error && <div className="studio-inline-error"><CircleAlert size={15} /><Markdown>{activity.error}</Markdown></div>}
    {onSelect && (!compact || target.kind !== 'change') && <button className="studio-text-button studio-activity-outcome" onClick={() => onSelect(target)}>{targetLabel}</button>}
    {!compact && <LazyDetails className="studio-activity-details" title={tr("原始详情")}><RawOperation item={item} />{native.map((record) => <LazyDetails key={record.id} title={tr("工具调用详情")}><RawOperation item={record} /></LazyDetails>)}</LazyDetails>}
  </section>
}

type Frame = { before: number | null; scrollTop: number }
type ReadingPosition = Frame & { history: Frame[] }
const freshPosition = (): ReadingPosition => ({ before: null, scrollTop: 0, history: [] })
function validFrame(value: unknown): value is Frame {
  if (!value || typeof value !== 'object') return false
  const frame = value as Frame
  return (frame.before === null || Number.isSafeInteger(frame.before) && frame.before > 0) && Number.isFinite(frame.scrollTop) && frame.scrollTop >= 0
}
function readPosition(key: string): ReadingPosition | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) ?? 'null')
    if (!validFrame(value)) return null
    const history: unknown = (value as ReadingPosition).history
    if (!Array.isArray(history) || !history.every(validFrame)) return null
    return { before: value.before, scrollTop: value.scrollTop, history }
  } catch { return null }
}
function writePosition(key: string, value: ReadingPosition) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* Reading remains available when browser storage is disabled. */ }
}

type TranscriptProps = { project: StudioProject; actorId: string; onSelect?: (selection: Selection) => void; header?: ReactNode }

export function Transcript(props: TranscriptProps) {
  useLocale()

  return <TranscriptSession key={`${props.project.id}:${props.actorId}`} {...props} />
}

function TranscriptSession({ project, actorId, onSelect, header }: TranscriptProps) {
  useLocale()

  const source = useStudioData()
  const role = project.actors?.[actorId]?.role
  const compactTools = role === 'executive' || role === 'set_director'
  const storageKey = `agentvideo:transcript:${encodeURIComponent(project.id ?? '')}:${encodeURIComponent(actorId)}`
  const [initial] = useState(() => readPosition(storageKey))
  const [position, setPosition] = useState<ReadingPosition>(() => initial ?? freshPosition())
  const [page, setPage] = useState<TranscriptPage | null>(null)
  const [loading, setLoading] = useState(Boolean(source))
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const ref = useRef<HTMLDivElement>(null)
  const follow = useRef(!initial)
  const allowFollow = useRef(!initial)
  const pendingScroll = useRef<number | null>(initial?.scrollTop ?? null)
  const reading = useRef(position)
  const [unseen, setUnseen] = useState(false)
  const before = position.before ?? undefined

  useEffect(() => {
    if (!source) return
    const controller = new AbortController()
    setLoading(true); setError('')
    void source.client.transcript(source.projectId, actorId, before, controller.signal).then((result) => {
      if (!controller.signal.aborted) setPage(result)
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "历史加载失败") }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [source?.client, source?.projectId, actorId, before, retry])

  const live = transcriptFor(project, actorId)
  const merged = before === undefined ? mergeTranscript(page?.items ?? [], live) : page?.items ?? []
  const items = (source ? merged : live).slice(-100)
  const moreBefore = Boolean(page?.has_more) || before === undefined && merged.length > 100
  const rows = activityRows(items)
  const last = items.at(-1)
  const lastVersion = `${last?.id ?? ''}:${last?.version ?? ''}:${last?.text ?? ''}:${last?.status ?? ''}`
  const previousVersion = useRef(lastVersion)

  const save = () => {
    reading.current = { ...reading.current, scrollTop: pendingScroll.current ?? ref.current?.scrollTop ?? reading.current.scrollTop }
    writePosition(storageKey, reading.current)
    return reading.current
  }
  const scrollBottom = () => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight
    pendingScroll.current = null
    follow.current = true
    allowFollow.current = true
    setUnseen(false)
    save()
  }
  const moveTo = (next: ReadingPosition, latest = false) => {
    follow.current = latest
    allowFollow.current = latest
    pendingScroll.current = latest ? null : next.scrollTop
    reading.current = next
    writePosition(storageKey, next)
    setPage(null); setPosition(next); setUnseen(false)
  }

  useLayoutEffect(() => {
    const element = ref.current
    if (!element || source && (!page || loading)) return
    if (pendingScroll.current !== null) {
      const top = pendingScroll.current
      element.scrollTop = top
      // Font loading and measured disclosure buttons can change line wraps in
      // the first commit. Finalize after that layout, before saving any drift.
      let active = true
      let frame: number | undefined
      const restore = () => {
        if (!active || pendingScroll.current !== top) return
        element.scrollTop = top
        pendingScroll.current = null
        previousVersion.current = lastVersion
      }
      const schedule = () => { if (active) frame = requestAnimationFrame(restore) }
      if (document.fonts?.status === 'loading') {
        void document.fonts.ready.then(schedule)
      } else schedule()
      return () => { active = false; if (frame !== undefined) cancelAnimationFrame(frame) }
    }
    if (follow.current) element.scrollTop = element.scrollHeight
    else if (before === undefined && previousVersion.current !== lastVersion) setUnseen(true)
    previousVersion.current = lastVersion
  }, [lastVersion, page, source, before, loading])

  useLayoutEffect(() => {
    const element = ref.current
    const persist = () => writePosition(storageKey, { ...reading.current, scrollTop: pendingScroll.current ?? element?.scrollTop ?? reading.current.scrollTop })
    window.addEventListener('pagehide', persist)
    return () => { persist(); window.removeEventListener('pagehide', persist) }
  }, [storageKey])

  const content = <div className="studio-transcript-container"><div className="studio-transcript" ref={ref} onWheel={() => { allowFollow.current = true }} onTouchMove={() => { allowFollow.current = true }} onPointerDown={() => { allowFollow.current = true }} onKeyDown={() => { allowFollow.current = true }} onScroll={() => {
    const element = ref.current
    if (element && pendingScroll.current === null && !(source && loading)) {
      follow.current = allowFollow.current && before === undefined && element.scrollHeight - element.scrollTop - element.clientHeight < 72
      if (follow.current) setUnseen(false)
      save()
    }
  }} aria-label={tr("导演输出")} aria-live={before === undefined && follow.current ? 'polite' : 'off'} aria-relevant="additions text">
    {header}
    <div className="studio-pagination">
      {moreBefore && <button className="studio-text-button" disabled={loading} onClick={() => {
        const cursor = before === undefined ? items[0]?.sequence || page?.next_cursor : page?.next_cursor
        if (cursor == null) return
        const current = save()
        moveTo({ before: cursor, scrollTop: 0, history: [...current.history, { before: current.before, scrollTop: current.scrollTop }] })
      }}>{tr("更早的输出")}</button>}
      {position.history.length > 0 && <button className="studio-text-button" disabled={loading} onClick={() => {
        const target = position.history.at(-1)!
        moveTo({ ...target, history: position.history.slice(0, -1) })
      }}>{tr("较新的输出")}</button>}
      {before !== undefined && <button className="studio-text-button" onClick={() => moveTo(freshPosition(), true)}>{tr("返回最新")}</button>}
    </div>
    {loading && <p role="status">{tr("正在加载历史…")}</p>}{error && <p role="alert">{trSystem(error)}<button className="studio-text-button" onClick={() => setRetry((value) => value + 1)}>{tr("重试")}</button></p>}
    {items.length ? rows.map((row) => row.kind === 'reads' ? <div className="studio-transcript-read-group" key={`reads:${row.items[0].id}`}>
      <LazyDetails className="studio-read-group" title={<><BookOpen size={15} /><strong>{tr("读取了")} {row.items.length} {tr("项资料")}</strong><span>{[...new Set(row.items.map((item) => activityFor(item).title))].join('、')}</span><ChevronDown size={14} /></>}>
        {row.items.map((item) => <article key={item.id} data-item-id={item.id}><Operation item={item} project={project} compact={compactTools} /></article>)}
      </LazyDetails>
    </div> : <article key={row.item.id} className={`studio-transcript-item studio-transcript-item--${row.item.kind}`} data-item-id={row.item.id}>
      {isOperation(row.item)
        ? <Operation item={row.item} project={project} compact={compactTools} onSelect={onSelect ? (selection) => { save(); onSelect(selection) } : undefined} native={row.native} />
        : row.item.kind === 'error' ? <>
          <div className="studio-inline-error"><CircleAlert size={16} /><Markdown>{row.item.text}</Markdown></div>
          <RoleRuntimeNotice item={row.item} />
        </> : <>
          {row.item.kind === 'user' && <span className="studio-message-label">{tr("你的要求")}</span>}
          <RoleRuntimeNotice item={row.item} />
          {compactTools && row.item.kind !== 'user' ? <ReadingText text={row.item.text} storageKey={`${storageKey}:message:${row.item.id}`} /> : <Markdown>{row.item.text}</Markdown>}
          {hasAdditionalOutput(row.item) && <LazyDetails title={tr("查看完整输出")}><BodyReader bodyRef={row.item.body_ref!} /></LazyDetails>}
          {row.item.status === 'running' && <span className="studio-writing" aria-label={tr("正在输出")} />}
        </>}
    </article>) : !loading && !error && <div className="studio-empty"><Terminal size={26} /><h3>{tr("等待开始工作")}</h3><p>{tr("导演的输出、工具调用和记忆更新会实时显示在这里。")}</p></div>}
  </div>{unseen && <button className="studio-new-output" onClick={scrollBottom}><ArrowDown size={14} />{tr("查看最新输出")}</button>}</div>
  return onSelect ? <EntityLinkProvider project={project} onSelect={(next) => { save(); onSelect(next) }}>{content}</EntityLinkProvider> : content
}

export { DetailValue }
