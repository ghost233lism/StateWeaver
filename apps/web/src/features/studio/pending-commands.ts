import type { ClipPreparationUpdate, CreateProject, UserAction } from './model'

type Requests = {
  action: UserAction
  create: CreateProject
  delete: UserAction
  preparation: ClipPreparationUpdate
}
type Command = keyof Requests
type Scope = readonly (string | boolean | null)[]
type Entry = { command: Command; scope: Scope; input: Requests[Command] }
/** A tab can restore the exact submitted intent after reload. Reads never submit it. */
export function pendingCommands(storageKey?: string) {
  const entries = new Map<string, Entry>()
  const key = (command: Command, scope: Scope) => JSON.stringify([command, scope])
  let persistedBytes: string | undefined
  const refresh = () => {
    if (!storageKey) return
    try {
      const bytes = sessionStorage.getItem(storageKey) ?? '[]'
      if (bytes === persistedBytes) return
      const saved = JSON.parse(bytes) as Entry[]
      if (!Array.isArray(saved)) return
      entries.clear()
      for (const entry of saved) {
        if (entry && ['action', 'create', 'delete', 'preparation'].includes(entry.command) &&
            Array.isArray(entry.scope) && typeof entry.input?.idempotency_key === 'string') {
          entries.set(key(entry.command, entry.scope), entry)
        }
      }
      persistedBytes = bytes
    } catch { /* Storage can be unavailable. Keep working in memory. */ }
  }
  const persist = () => {
    if (!storageKey) return
    try {
      const bytes = JSON.stringify([...entries.values()])
      if (entries.size) sessionStorage.setItem(storageKey, bytes)
      else sessionStorage.removeItem(storageKey)
      persistedBytes = bytes
    } catch { /* Storage can be unavailable. Keep working in memory. */ }
  }
  return {
    get<K extends Command>(command: K, scope: Scope): Requests[K] | undefined {
      refresh()
      return entries.get(key(command, scope))?.input as Requests[K] | undefined
    },
    latest<K extends Command>(command: K, prefix: Scope): Requests[K] | undefined {
      refresh()
      for (const entry of [...entries.values()].reverse()) {
        if (entry.command === command && prefix.every((part, index) => entry.scope[index] === part)) {
          return entry.input as Requests[K]
        }
      }
    },
    save<K extends Command>(command: K, scope: Scope, input: Requests[K]) {
      refresh()
      const intent = key(command, scope)
      entries.delete(intent)
      entries.set(intent, { command, scope, input })
      persist()
    },
    forget(command: Command, scope: Scope, idempotencyKey?: string) {
      refresh()
      const intent = key(command, scope)
      if (idempotencyKey && entries.get(intent)?.input.idempotency_key !== idempotencyKey) return
      if (entries.delete(intent)) persist()
    },
  }
}

export type PendingCommands = ReturnType<typeof pendingCommands>
