import { tr, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState } from 'react'
import { ImageOff } from 'lucide-react'
import { useStudioImagePreviewUrl } from './image-preview'
import { useEntityDetail } from './data'
import { Markdown } from './Markdown'
import { viewLabel, referenceName, type Reference, type Selection, type StudioProject } from './model'
import { EntityLink, EntityLinkProvider } from './EntityLinks'
import { entityExcerpt } from './entity-link-model'
import type { OperationChange } from './operation-change-model'
import './OperationChanges.css'

type Select = { onSelect?: (selection: Selection) => void }

function ChangeText({ text }: { text: string }) {
  useLocale()

  const [expanded, setExpanded] = useState(false)
  const paragraph = text.split(/\n\s*\n/)[0]
  const preview = entityExcerpt(paragraph, 280)
  const shortened = preview.length < text.length
  return <div className="studio-change-text"><Markdown>{expanded || !shortened ? text : `${preview}${preview.length < paragraph.length ? '…' : ''}`}</Markdown>
    {shortened && <button className="studio-text-button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? tr("收起摘要") : tr("展开摘要")}</button>}
  </div>
}

export function OperationChanges({ change, project, onSelect }: { change: OperationChange; project: StudioProject } & Select) {
  useLocale()

  return <div className="studio-operation-changes">
    <div className="studio-change-meta"><span>{change.scope}</span><span>{change.category}</span>
      {change.revisionBefore !== undefined && change.revisionAfter !== undefined ? <span>{tr("版本")} {change.revisionBefore} → {change.revisionAfter}</span>
        : change.revisionAfter !== undefined && <span>{tr("版本")} {change.revisionAfter}</span>}
    </div>
    {change.summary ? <ChangeText text={change.summary} /> : change.action === 'write_memory' && <p className="studio-change-note">{tr("本次更新未附内容摘要。")}</p>}
    {change.bindings && <BindingChanges bindings={change.bindings} project={project} onSelect={onSelect} />}
    {change.reference && <ReferenceChanges reference={change.reference} action={change.action} showReason={!change.summary} project={project} onSelect={onSelect} />}
  </div>
}

function BindingChanges({ bindings, project, onSelect }: { bindings: NonNullable<OperationChange['bindings']>; project: StudioProject } & Select) {
  useLocale()

  const [expanded, setExpanded] = useState(false)
  const before = new Set(bindings.before)
  const after = new Set(bindings.after)
  const records = bindings.before && !bindings.truncated
    ? [...bindings.after.filter((id) => !before.has(id)).map((id) => ({ id, label: tr("新增关联") })), ...bindings.before.filter((id) => !after.has(id)).map((id) => ({ id, label: tr("移除关联") }))]
    : bindings.after.map((id) => ({ id, label: tr("本次关联") }))
  return <div className="studio-change-bindings">
    <p>{tr("关联资产状态")}{bindings.countBefore !== undefined ? tr(" {{0}} → {{1}} 项", bindings.countBefore, bindings.countAfter) : tr(" {{0}} 项", bindings.countAfter)}{bindings.before && !bindings.truncated && !records.length ? tr("，关联未改变") : ''}</p>
    <ul>{(expanded ? records : records.slice(0, 3)).map(({ id, label }) => {
      const state = project.states?.[id]
      const asset = state ? project.assets?.[state.asset_id] : undefined
      const name = [asset?.name, state?.label].filter(Boolean).join(' · ') || tr("关联资产状态")
      return <li key={`${label}:${id}`}><span>{label}</span>{state && onSelect ? <button className="studio-text-button" onClick={() => onSelect({ kind: 'asset', id: state.asset_id, stateId: id })}>{name}</button> : <EntityLink id={id}>{name}</EntityLink>}</li>
    })}</ul>
    {records.length > 3 && <button className="studio-text-button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? tr("收起关联状态") : tr("展开 {{0}} 项关联状态", records.length)}</button>}
    {bindings.truncated && <p className="studio-change-note">{tr("这里显示部分关联状态，共")} {bindings.countAfter} {tr("项。")}</p>}
  </div>
}

/** Only references whose comparison entered the viewport may request scoped details. */
function useSeenComparison() {
  const ref = useRef<HTMLDivElement>(null)
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    if (seen || !ref.current) return
    if (typeof IntersectionObserver === 'undefined') { setSeen(true); return }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setSeen(true); observer.disconnect() }
    })
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [seen])
  return { ref, seen }
}

function missingDetail(project: StudioProject, id: string) {
  const reference = project.references?.[id]
  return !reference || reference.status === 'pending' || Boolean(reference.media_id && !project.media?.[reference.media_id])
}

function ReferenceChanges({ reference, action, showReason, project, onSelect }: { reference: NonNullable<OperationChange['reference']>; action: string; showReason: boolean; project: StudioProject } & Select) {
  useLocale()

  const { ref, seen } = useSeenComparison()
  const afterDetail = useEntityDetail(project, seen && reference.id && missingDetail(project, reference.id) ? `reference:${reference.id}` : undefined)
  const after = afterDetail.project.references?.[reference.id]
  // A recorded null means first selection. Never substitute the current selection.
  const beforeId = reference.beforeKnown ? reference.beforeId : after ? after.parent_id : reference.parentId
  const beforeDetail = useEntityDetail(afterDetail.project, seen && beforeId && beforeId !== reference.id && missingDetail(afterDetail.project, beforeId) ? `reference:${beforeId}` : undefined)
  const before = beforeId ? beforeDetail.project.references?.[beforeId] : undefined
  const stateId = after?.state_id || reference.stateId
  const view = after?.view || reference.view
  const sameView = Boolean(after && before && stateId && view && before.state_id === stateId && before.view === view)
  const state = beforeDetail.project.states?.[stateId]
  const assetId = reference.assetId || state?.asset_id
  const asset = assetId ? beforeDetail.project.assets?.[assetId] : undefined
  const context = [asset?.name, state?.label, view ? viewLabel(view) : ''].filter(Boolean).join(' · ')
  const selecting = action === 'select_reference'
  const beforeLabel = reference.beforeKnown ? selecting ? tr("选用前") : tr("生成前选用") : tr("修改来源")
  const afterLabel = selecting ? tr("选用后") : tr("本次生成")
  const beforeEmpty = reference.beforeKnown && reference.beforeId === null ? tr("首次选用，之前没有选用的参考图")
    : after && before && !sameView ? tr("没有同一剧情状态与视图的旧图")
      : beforeId ? tr("该版参考图暂不可用") : reference.beforeKnown ? tr("未记录更新前的参考图") : tr("没有可核对的修改来源")
  const content = <div ref={ref} className="studio-reference-change" data-reference-change={reference.id}>
    {context && <p className="studio-change-context">{context}</p>}
    {showReason && after?.reason && <ChangeText text={after.reason} />}
    <div className="studio-change-images">
      <ReferenceImage id={beforeId ?? ''} reference={sameView ? before : undefined} project={beforeDetail.project} label={beforeLabel} context={context} visible={seen} empty={beforeEmpty} onSelect={onSelect} />
      <ReferenceImage id={reference.id} reference={after} project={beforeDetail.project} label={afterLabel} context={context} visible={seen} empty={reference.id ? tr("该版参考图暂不可用") : tr("本次操作未记录参考图")} onSelect={onSelect} />
    </div>
    {selecting && reference.beforeKnown && beforeId === reference.id && <p className="studio-change-note">{tr("继续选用同一版参考图。")}</p>}
    {selecting && !reference.beforeKnown && beforeId && sameView && <p className="studio-change-note">{tr("左图是这版参考图的修改来源；历史记录未记载选用前的图片。")}</p>}
    {afterDetail.status}{beforeDetail.status}
  </div>
  return onSelect ? <EntityLinkProvider project={beforeDetail.project} onSelect={onSelect}>{content}</EntityLinkProvider> : content
}

function ReferenceImage({ id, reference, project, label, context, visible, empty, onSelect }: { id: string; reference?: Reference; project: StudioProject; label: string; context: string; visible: boolean; empty: string } & Select) {
  useLocale()

  const media = reference?.media_id ? project.media?.[reference.media_id] : undefined
  const imagePreviewUrl = useStudioImagePreviewUrl()
  const src = media?.mime_type.startsWith('image/') ? imagePreviewUrl(media.url) : ''
  const [failedSrc, setFailedSrc] = useState('')
  const failed = Boolean(src && src === failedSrc)
  const placeholder = failed ? tr("图片加载失败") : reference?.status === 'pending' ? tr("生成中，结果回来后显示") : reference?.status === 'failed' ? tr("该次参考图生成失败") : empty
  const title = `${context ? `${context} · ` : ''}${label}`
  return <figure className="studio-change-image"><figcaption><span>{label}</span>{id && <small><EntityLink id={id}>{reference ? referenceName(reference) : tr("查看这版参考图")}</EntityLink></small>}</figcaption>
    {visible && src && !failed ? onSelect ? <button className="studio-change-image-button" aria-label={tr("查看{{0}}参考图", label)} data-reference-id={id} onClick={() => onSelect({ kind: 'reference', id, referenceId: id, stateId: reference!.state_id })}><img src={src} alt={title} loading="lazy" onError={() => setFailedSrc(src)} /></button>
      : <img className="studio-change-image-static" src={src} alt={title} loading="lazy" onError={() => setFailedSrc(src)} />
      : <div className="studio-change-image-empty" role={reference?.status === 'failed' || failed ? 'status' : undefined}><ImageOff size={20} /><span>{visible ? placeholder : tr("图片将在此处显示")}</span>{failed && <button className="studio-text-button" onClick={() => setFailedSrc('')}>{tr("重新加载图片")}</button>}</div>}
    {visible && src && !failed && reference?.status === 'failed' && <p className="studio-change-note" role="status">{tr("该次参考图生成失败")}</p>}
  </figure>
}
