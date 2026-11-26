import { createContext, useContext } from 'react'
import { mediaUrl, type StudioClient } from './client'
import type { ProjectCache } from './cache'
import type { PendingCommands } from './pending-commands'

/** Shared by the library shell and deferred detail readers without importing
 * Markdown, inspection panels or canvas code into the initial page. */
export const StudioDataContext = createContext<{ client: StudioClient; projectId: string; currentCache?: () => ProjectCache | null; invalidations?: Record<string, number>; pending?: PendingCommands; checkMissingRoute?: (reason: unknown) => Promise<void> } | null>(null)
export const useStudioData = () => useContext(StudioDataContext)
export const StudioMediaUrlContext = createContext<(url: string) => string>(mediaUrl)
export const useStudioMediaUrl = () => useContext(StudioMediaUrlContext)
