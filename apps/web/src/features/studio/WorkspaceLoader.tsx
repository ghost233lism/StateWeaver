import { tr, useLocale } from '../../ui/locale'
import { Component, lazy, Suspense, type ComponentProps, type ReactNode } from 'react'
import { LoaderCircle } from 'lucide-react'

let workspace: ReturnType<typeof importWorkspace> | undefined
function importWorkspace() { return import('./StudioWorkspace') }
const loadWorkspace = () => workspace ??= importWorkspace()
const Workspace = lazy(loadWorkspace)

/** Fetch the workspace alongside the project read, or when a project is hovered.
 * The library itself never imports canvas, editor or review implementations. */
export function preloadWorkspace() { void loadWorkspace().catch(() => {}) }

class WorkspaceBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed ? <div className="studio-page-loading" role="alert"><p>{tr("工作区暂时无法加载，请检查连接后重新加载。")}</p><button className="studio-button" onClick={() => window.location.reload()}>{tr("重新加载")}</button></div> : this.props.children
  }
}

export function LazyWorkspace(props: ComponentProps<typeof Workspace>) {
  useLocale()

  return <WorkspaceBoundary key={props.project.id}><Suspense fallback={<div className="studio-page-loading" role="status"><LoaderCircle className="studio-spinner" />{tr("正在打开工作区…")}</div>}><Workspace {...props} /></Suspense></WorkspaceBoundary>
}
