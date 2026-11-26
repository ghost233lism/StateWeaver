import { tr, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowUp, Check, FileText, LockKeyhole, MessageSquare, PanelLeftClose } from 'lucide-react'
import { entries, type DirectorMode, type StudioProject } from './model'
import { Status } from './Status'
import { Transcript } from './Transcript'
import { Markdown } from './Markdown'
import { useScreenplaySidebar } from './screenplay-sidebar'
import { useStudioData } from './dataContext'
import './Screenplay.css'

type Props = {
  project: StudioProject; busy: boolean; onMessage: (text: string) => Promise<boolean>; onConfirm: (mode?: DirectorMode) => void
}

export function ScreenplayStudio(props: Props) {
  useLocale()

  return <ScreenplayWorkspace key={props.project.id} {...props} />
}

function ScreenplayWorkspace({ project, busy, onMessage, onConfirm }: Props) {
  useLocale()

  const source = useStudioData()
  const recovered = source?.pending?.latest('action', [project.id ?? source.projectId, 'message'])
  const [message, setMessage] = useState(recovered?.text ?? '')
  const [outline, setOutline] = useState<{ title: string; level: number }[]>([])
  const script = useRef<HTMLDivElement>(null)
  const conversation = useRef<HTMLElement>(null)
  const toggleButton = useRef<HTMLButtonElement>(null)
  const scriptText = project.screenplay?.text ?? ''
  const writer = entries(project.actors).find((actor) => actor.role === 'screenwriter')
  const frozen = Boolean(project.screenplay?.confirmed_at) || project.status !== 'screenwriting'
  const mode = project.director_mode ?? 'delegated-v1'
  const confirmation = source?.pending?.latest('action', [project.id ?? source.projectId, 'confirm'])
  const [selectedMode, setSelectedMode] = useState<DirectorMode>(confirmation?.director_mode ?? mode)
  const sidebar = useScreenplaySidebar(project.id ?? '', frozen)
  const estimate = project.screenplay?.manuscript?.duration_estimate
  const duration = estimate ? tr("{{0}}{{1}} 秒", estimate.minimum_seconds, estimate.maximum_seconds === estimate.minimum_seconds ? '' : `–${estimate.maximum_seconds}`) : null
  const headingSelector = '.studio-script-text h1, .studio-script-text h2, .studio-script-text h3'
  useEffect(() => {
    setOutline(Array.from(script.current?.querySelectorAll<HTMLElement>(headingSelector) ?? []).map((heading) => ({ title: heading.textContent ?? '', level: Number(heading.tagName.slice(1)) })))
  }, [scriptText])
  const jumpTo = (index: number) => {
    const heading = script.current?.querySelectorAll<HTMLElement>(headingSelector)[index]
    heading?.scrollIntoView({ behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
    heading?.setAttribute('tabindex', '-1')
    heading?.focus({ preventScroll: true })
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const submitted = message.trim()
    if (!submitted || busy) return
    if (await onMessage(submitted)) setMessage((current) => current.trim() === submitted ? '' : current)
  }
  const toggleConversation = () => {
    sidebar.toggle()
    if (!sidebar.open && window.innerWidth <= 900) requestAnimationFrame(() => {
      const input = conversation.current?.querySelector<HTMLTextAreaElement>('textarea:not(:disabled)')
      const target = input ?? conversation.current?.querySelector<HTMLElement>('h2')
      target?.focus({ preventScroll: true })
      conversation.current?.scrollIntoView?.({ block: 'start', behavior: 'instant' })
    })
  }
  const toggleLabel = frozen ? (sidebar.open ? tr("收起创作记录") : tr("查看创作记录")) : (sidebar.open ? tr("收起创作对话") : tr("展开创作对话"))
  return <section ref={sidebar.root} style={sidebar.style} className={`studio-screenplay${frozen ? ' is-confirmed' : ''}${sidebar.open ? ' has-conversation' : ''}${sidebar.resizing ? ' is-resizing' : ''}`} aria-label={tr("编剧工作台")}>
    <aside ref={conversation} id="studio-writing-conversation" className="studio-writing-room" hidden={!sidebar.open} aria-label={frozen ? tr("编剧创作记录") : tr("创作对话")}>
      {sidebar.mounted && <>{sidebar.handle}<header><div><h2 tabIndex={-1}>{frozen ? tr("编剧创作记录") : tr("和编剧一起写故事")}</h2><p>{frozen ? tr("保留讨论与修订，剧本正文已确认。") : tr("先确定故事，再交给导演制作。")}</p></div><button className="studio-icon" aria-label={frozen ? tr("关闭创作记录") : tr("关闭创作对话")} onClick={() => { sidebar.toggle(); toggleButton.current?.focus() }}><PanelLeftClose size={17} /></button>{writer && (!frozen && writer.status === 'waiting' && !writer.wake_requested && !writer.paused ? <span className="studio-status">{tr("等待你审阅")}</span> : <Status value={writer.status} />)}</header>
        {writer ? <Transcript project={project} actorId={writer.id} /> : <p className="studio-empty-line">{tr("编剧会话正在准备。")}</p>}
        {!frozen && <form className="studio-message-form" onSubmit={submit}><label htmlFor="studio-writer-message">{tr("补充创作要求")}</label><textarea id="studio-writer-message" rows={3} value={message} onChange={(event) => setMessage(event.target.value)} placeholder={tr("告诉编剧你希望调整的剧情、人物或台词…")} disabled={busy} /><div><span>{tr("创作要求统一在这里提出")}</span><button className="studio-writer-send" aria-label={tr("发送")} title={busy ? tr("正在发送…") : tr("发送创作要求")} aria-busy={busy} disabled={busy || !message.trim()} type="submit"><ArrowUp size={20} aria-hidden="true" /></button></div></form>}
      </>}
    </aside>
    <div className="studio-script"><header><FileText size={20} /><h2>{frozen ? tr("已确认剧本") : tr("剧本草稿")}</h2>{frozen && <LockKeyhole size={16} />}{duration && <span className="studio-script-estimate">{tr("预计")} {duration}</span>}<button ref={toggleButton} className="studio-text-button studio-writing-history-toggle" aria-expanded={sidebar.open} aria-controls="studio-writing-conversation" onClick={toggleConversation}>{sidebar.open ? <PanelLeftClose size={17} /> : <MessageSquare size={17} />}{toggleLabel}</button></header>
      <div className="studio-script-reading">
      {outline.length > 0 && <nav className="studio-script-outline" aria-label={tr("剧本目录")}><span>{tr("阅读目录")}</span><select aria-label={tr("跳转剧本章节")} defaultValue="" onChange={(event) => jumpTo(Number(event.target.value))}><option value="" disabled>{tr("跳转章节")}</option>{outline.map(({ title }, index) => <option key={index} value={index}>{title}</option>)}</select><ol>{outline.map(({ title, level }, index) => <li key={index} className={level === 3 ? 'is-paragraph' : ''}><button onClick={() => jumpTo(index)}>{title}</button></li>)}</ol></nav>}
      <div className="studio-script-body" ref={script} tabIndex={0} aria-label={tr("剧本正文")}><div className="studio-script-paper">{scriptText ? <>
        {!/^#\s+\S/.test(scriptText.trimStart()) && <h1>{project.title}</h1>}<Markdown className="studio-script-text">{scriptText}</Markdown>
      </> : <div className="studio-empty"><FileText size={36} /><h3>{tr("故事从这里开始")}</h3><p>{tr("编剧会根据你的要求形成剧本，完成后会显示在这里供你确认。")}</p><blockquote><Markdown>{project.brief}</Markdown></blockquote></div>}</div></div></div>
      <footer>
        {frozen ? <>
          <p><Check size={17} />{tr("已确认。导演将依据这份剧本继续制作。")}</p>
          <span className="studio-director-mode-locked"><LockKeyhole size={14} />{mode === 'delegated-v1' ? tr("委托模式") : tr("现有模式")} {tr("· 已锁定")}</span>
        </> : <>
          <p>{tr("确认后故事固定，总导演划分片场，片场导演规划 Clip。")}</p>
          <div className="studio-screenplay-start">
            <label className="studio-director-mode">{tr("制作模式")}
              <select aria-label={tr("制作模式")} disabled={busy} value={selectedMode} onChange={(event) => setSelectedMode(event.target.value as DirectorMode)}>
                <option value="delegated-v1">{tr("委托模式")}</option>
                <option value="current">{tr("现有模式")}</option>
              </select>
            </label>
            <button className="studio-button studio-button--primary" onClick={() => onConfirm(selectedMode)} disabled={busy || !scriptText.trim() || !estimate || writer?.status !== 'waiting' || writer?.paused || writer?.wake_requested}>
              <Check size={17} />{tr("确认剧本并开始制作")}
            </button>
          </div>
        </>}
      </footer>
    </div>
  </section>
}
