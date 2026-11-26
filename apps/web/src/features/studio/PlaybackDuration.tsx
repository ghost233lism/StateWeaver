import { tr, trSystem, useLocale } from '../../ui/locale'
import type { ProductionStatus } from './model'

type Playback = NonNullable<ProductionStatus['playback']>

export function playbackTime(seconds: number) {
  const total = Math.round(seconds)
  if (total < 60) return tr("{{0}} 秒", total)
  const minutes = Math.floor(total / 60), remaining = total % 60
  return remaining ? tr("{{0}} 分 {{1}} 秒", minutes, remaining) : tr("{{0}} 分钟", minutes)
}

export function PlaybackSummary({ playback }: { playback: Playback }) {
  useLocale()

  return <span className="studio-production-playback">
    {playback.final_actual_seconds != null ? tr("成片片长 {{0}}", playbackTime(playback.final_actual_seconds))
      : `${playback.plan_complete ? tr("规划片长") : tr("已规划片长")} ${playbackTime(playback.planned_seconds)}`}
    {!playback.plan_complete && playback.final_actual_seconds == null && <small>{tr("规划未齐")}</small>}
  </span>
}

const statusLabels: Record<string, string> = {
  get unplanned() { return tr("待拆分 Clip") }, get incomplete() { return tr("部分规划") }, get within_budget() { return tr("区间内") },
  get needs_budget() { return tr("待总导演分配") }, get below_minimum() { return tr("待下调区间") }, get above_maximum() { return tr("待调整规划或区间") },
  get display_only() { return tr("历史对账") },
}

export function PlaybackDetails({ playback }: { playback: Playback }) {
  useLocale()

  const excess = playback.estimate ? playback.planned_seconds - playback.estimate.maximum_seconds : 0
  return <details className="studio-playback-detail">
    <summary>{tr("影片播放时长 ·")} {playbackTime(playback.planned_seconds)}{!playback.plan_complete && tr("（已知规划）")}</summary>
    <p>{playback.estimate ? tr("确认剧本预估 {{0}}–{{1}}", playbackTime(playback.estimate.minimum_seconds), playbackTime(playback.estimate.maximum_seconds)) : tr("确认剧本暂无整体时长预估")}；{playback.plan_complete ? tr("完整规划") : tr("已知规划")} {playbackTime(playback.planned_seconds)}。</p>
    {excess > 0 && <p className="studio-production-note">{playback.plan_complete ? tr("当前规划") : tr("已知规划")}{tr("比剧本预估上限多")} {playbackTime(excess)}{tr("，由总导演结合剧情与自然节奏统筹。")}</p>}
    {playback.estimate?.basis && <details className="studio-playback-basis"><summary>{tr("剧本预估依据")}</summary><p className="studio-production-note">{playback.estimate.basis}</p></details>}
    <p>{tr("已选片实际时长合计")} {playbackTime(playback.selected_actual_seconds)}{playback.selected_missing_duration ? tr("（另有 {{0}} 条尚无时长记录）", playback.selected_missing_duration) : ''}{tr("；成片实际时长")} {playback.final_actual_seconds == null ? tr("待合成") : playbackTime(playback.final_actual_seconds)}。</p>
    {!playback.plan_complete && <p className="studio-production-note">{playback.unplanned_set_ids?.length ?? 0} {tr("个 Set 的 Clip 规划尚未完成，当前合计不代表整片总长。")}</p>}
    {playback.policy === 'display-only' && <p className="studio-production-note">{tr("历史项目仅展示对账，沿用原生产规则。")}</p>}
    <div className="studio-production-table"><table>
      <thead><tr><th>{tr("片场")}</th><th>{tr("目标 / 允许区间")}</th><th>{tr("规划或原提交")}</th><th>{tr("已选实际")}</th><th>{tr("状态")}</th></tr></thead>
      <tbody>{playback.sets?.map((set) => <tr key={set.set_id}>
        <th>{set.title}</th>
        <td>{set.budget ? `${playbackTime(set.budget.target_seconds)} / ${playbackTime(set.budget.minimum_seconds)}–${playbackTime(set.budget.maximum_seconds)}` : tr("未分配")}</td>
        <td>{playbackTime(set.accounted_seconds)}{!set.plan_complete && tr("（未齐）")}</td>
        <td>{playbackTime(set.selected_actual_seconds)}{set.selected_missing_duration ? tr("（记录未齐）") : ''}</td>
        <td>{statusLabels[set.status] ?? set.status}{set.reason && <span className="studio-playback-reason">{set.reason} {tr("· 总导演")}</span>}</td>
      </tr>)}</tbody>
    </table></div>
    {playback.notes?.map((note) => <p className="studio-production-note" key={note}>{trSystem(note)}</p>)}
  </details>
}
