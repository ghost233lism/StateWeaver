import { useEffect, useState } from 'react'

/** Browser reading preferences never alter shared layout or production memory. */
function storedDisclosure(projectId: string): Record<string, boolean> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(`studio:memory-disclosure:${projectId}`) ?? '{}')
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
    return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean'))
  } catch { return {} }
}

export function useMemoryDisclosure(projectId: string) {
  const [disclosure, setDisclosure] = useState(() => storedDisclosure(projectId))
  useEffect(() => {
    setDisclosure(storedDisclosure(projectId))
    const update = (event: Event) => {
      const detail = (event as CustomEvent<{ projectId: string; next: Record<string, boolean> }>).detail
      if (detail?.projectId === projectId) setDisclosure(detail.next)
    }
    const storage = (event: StorageEvent) => { if (event.key === `studio:memory-disclosure:${projectId}`) setDisclosure(storedDisclosure(projectId)) }
    window.addEventListener('studio-memory-disclosure', update)
    window.addEventListener('storage', storage)
    return () => { window.removeEventListener('studio-memory-disclosure', update); window.removeEventListener('storage', storage) }
  }, [projectId])
  return {
    expanded: (key: string, automatic = false) => disclosure[key] ?? automatic,
    setExpanded: (key: string, expanded: boolean) => {
      const next = { ...disclosure, ...storedDisclosure(projectId), [key]: expanded }
      try { localStorage.setItem(`studio:memory-disclosure:${projectId}`, JSON.stringify(next)) } catch { /* Reading still works without storage. */ }
      setDisclosure(next)
      window.dispatchEvent(new CustomEvent('studio-memory-disclosure', { detail: { projectId, next } }))
    },
  }
}
