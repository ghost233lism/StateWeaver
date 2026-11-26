import { tr, useLocale } from '../../ui/locale'
import { useCallback, useId, useRef, useState, type KeyboardEvent } from 'react'
import { NodeResizeControl, NodeResizer, type NodeProps, type NodeResizerProps } from '@xyflow/react'
import { BookOpen, GripHorizontal, Maximize2, MoveDiagonal2 } from 'lucide-react'
import { GlobalMemory } from './GlobalMemory'
import { STUDIO_MEMORY_WIDTH, type StudioNode } from './projection'
import { FullscreenControl } from './Fullscreen'
import './GlobalMemoryNode.css'

type MemoryNodeData = StudioNode['data'] & {
  manualHeight?: number | null
  onSizeChange?: (width: number, height: number | null) => void
  storyWidth?: number | null
  onStoryWidthChange?: (width: number | null) => void
}

const limits = { minWidth: 680, minHeight: 190, maxHeight: 2400 }
const bounded = (value: number, minimum: number, maximum = Infinity) => Math.max(minimum, Math.min(maximum, Math.round(value)))

export function GlobalMemoryNode({ data: nodeData, width, height, draggable }: NodeProps<StudioNode>) {
  useLocale()

  const data = nodeData as MemoryNodeData
  const [resizing, setResizing] = useState(false)
  const helpId = useId()
  const onSizeChange = useRef(data.onSizeChange)
  onSizeChange.current = data.onSizeChange
  const canResize = draggable && Boolean(data.onSizeChange)
  const fixedHeight = resizing || data.manualHeight != null
  // Stable handlers keep React Flow's drag session alive during dimension updates.
  const beginResize = useCallback<NonNullable<NodeResizerProps['onResizeStart']>>(() => setResizing(true), [])
  const finishResize = useCallback<NonNullable<NodeResizerProps['onResizeEnd']>>((_, size) => {
    setResizing(false)
    onSizeChange.current?.(bounded(size.width, limits.minWidth), bounded(size.height, limits.minHeight, limits.maxHeight))
  }, [])
  const resizeByKeyboard = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!canResize || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
    event.preventDefault(); event.stopPropagation()
    const step = event.shiftKey ? 80 : 20
    const nextWidth = (width ?? STUDIO_MEMORY_WIDTH) + (event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0)
    const nextHeight = (height ?? data.manualHeight ?? limits.minHeight) + (event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0)
    onSizeChange.current?.(bounded(nextWidth, limits.minWidth), bounded(nextHeight, limits.minHeight, limits.maxHeight))
  }
  return <div className={`studio-memory-workspace studio-memory-node${fixedHeight ? ' studio-memory-node--fixed' : ''}${resizing ? ' is-resizing' : ''}`}>
    <NodeResizer {...limits} isVisible={canResize} handleClassName="studio-memory-resize-handle" lineClassName="studio-memory-resize-edge" onResizeStart={beginResize} onResizeEnd={finishResize} />
    <div className="studio-memory-drag" title={tr("拖动标题移动全局记忆")}><GripHorizontal size={17} /><BookOpen size={18} /><strong>{tr("全局记忆")}</strong><span>{tr("全片共享")}</span>
      <button className="studio-text-button studio-memory-fit nodrag" disabled={!canResize || data.manualHeight == null} title={tr("保留宽度，让高度跟随展开内容")} onClick={(event) => { event.stopPropagation(); onSizeChange.current?.(width ?? STUDIO_MEMORY_WIDTH, null) }}><Maximize2 size={13} />{tr("适应内容")}</button>
      <FullscreenControl mode="memory" className="nodrag" />
    </div>
    <div className="studio-memory-node-content nodrag nowheel">{data.project && data.onSelect && <GlobalMemory project={data.project} selectedClipId={data.selectedClipId} onSelect={data.onSelect} assetOrder={data.assetOrder ?? {}} onAssetOrderChange={data.onAssetOrderChange ?? (() => {})} storyWidth={data.storyWidth} onStoryWidthChange={canResize ? data.onStoryWidthChange : undefined} fillHeight={fixedHeight} />}</div>
    {canResize && <NodeResizeControl {...limits} position="bottom-right" className="studio-memory-resize-corner" onResizeStart={beginResize} onResizeEnd={finishResize}>
      <button className="studio-memory-resize-keyboard nodrag nopan" aria-label={tr("调整全局记忆大小")} aria-describedby={helpId} title={tr("拖动调整宽高；方向键调节，Shift 加速")} onKeyDown={resizeByKeyboard}><MoveDiagonal2 size={16} aria-hidden="true" /></button>
    </NodeResizeControl>}
    <span id={helpId} className="studio-memory-resize-help">{tr("拖动边缘或角落调整大小。聚焦右下角后，左右方向键调整宽度，上下方向键调整高度；按住 Shift 可加大步长。")}</span>
  </div>
}
