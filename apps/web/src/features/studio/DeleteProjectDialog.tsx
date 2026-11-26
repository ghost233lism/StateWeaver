import { tr, trSystem, useLocale } from '../../ui/locale'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { LoaderCircle, Trash2, X } from 'lucide-react'
import { confirmsCommand, isDefiniteCommandRejection, operationKey, StudioApiError, type StudioClient } from './client'
import type { ProjectSummary } from './model'
import type { PendingCommands } from './pending-commands'

export function DeleteProjectDialog({ project, client, commands, onRequestError, onClose, onDeleted }: {
  project: ProjectSummary
  client: StudioClient
  commands: PendingCommands
  onRequestError: (reason: unknown) => Promise<void>
  onClose: () => void
  onDeleted: () => void
}) {
  useLocale()

  const dialog = useRef<HTMLDialogElement>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)
  const previousFocus = useRef(document.activeElement)
  const pending = useRef(false)
  const mounted = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    mounted.current = true
    const element = dialog.current
    element?.showModal()
    cancelButton.current?.focus()
    return () => {
      mounted.current = false
      element?.close()
      if (previousFocus.current instanceof HTMLElement && previousFocus.current.isConnected) previousFocus.current.focus()
    }
  }, [])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (pending.current || !client.deleteProject) return
    pending.current = true
    setBusy(true); setError('')
    const intent = [project.id]
    const attempted = commands.get('delete', intent)
    const request = attempted ?? { expected_revision: project.revision, idempotency_key: operationKey(), text: '' }
    const deleted = () => {
      commands.forget('delete', intent, request.idempotency_key)
      if (mounted.current) onDeleted()
    }
    try {
      commands.save('delete', intent, request)
      const result = await client.deleteProject(project.id, request)
      if (!confirmsCommand(result, project.id)) throw new Error("未能确认项目删除，请重试。")
      deleted()
    } catch (reason) {
      if (reason instanceof StudioApiError && reason.status === 410) { deleted(); return }
      void onRequestError(reason)
      if (reason instanceof StudioApiError && reason.status === 409) {
        try {
          const latest = await client.get(project.id)
          commands.save('delete', intent, { ...request, expected_revision: latest.project.revision })
          setError("项目有新进展，已更新状态。请再次确认删除。")
        } catch (refreshError) {
          if (refreshError instanceof StudioApiError && refreshError.status === 410) { deleted(); return }
          void onRequestError(refreshError)
          setError("暂时无法读取项目最新状态，请稍后重试。")
        }
      } else {
        if (isDefiniteCommandRejection(reason)) commands.forget('delete', intent, request.idempotency_key)
        setError(reason instanceof Error ? reason.message : "删除失败，请重试。")
      }
    } finally { pending.current = false; setBusy(false) }
  }

  return <dialog ref={dialog} className="studio-create-dialog studio-delete-dialog" aria-labelledby="studio-delete-title" aria-describedby="studio-delete-description" onCancel={(event) => { event.preventDefault(); if (!pending.current) onClose() }}>
    <form onSubmit={(event) => { void submit(event) }}>
      <header><h2 id="studio-delete-title">{tr("删除“")}{project.title}”？</h2><button type="button" className="studio-icon" onClick={onClose} disabled={busy} aria-label={tr("关闭删除项目")}><X size={20} /></button></header>
      <p id="studio-delete-description">{tr("项目将从“我的项目”移除，并停止后续制作。")}</p>
      {error && <p className="studio-error-text" role="alert">{trSystem(error)}</p>}
      <footer><button ref={cancelButton} type="button" className="studio-button" autoFocus disabled={busy} onClick={onClose}>{tr("取消")}</button><button type="submit" className="studio-button studio-button--danger" disabled={busy}>{busy ? <LoaderCircle size={16} className="studio-spinner" /> : <Trash2 size={16} />}{busy ? tr("正在删除…") : tr("删除项目")}</button></footer>
    </form>
  </dialog>
}
