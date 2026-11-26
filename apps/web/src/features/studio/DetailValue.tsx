import { tr, useLocale } from '../../ui/locale'
import { Markdown } from './Markdown'
import { EntityText } from './EntityLinks'

export const actionLabels: Record<string, string> = {
  get get_project() { return tr("读取画布") }, get act() { return tr("执行操作") }, get read_media() { return tr("查看素材") }, get get_history() { return tr("读取历史") },
  get get_required_context() { return tr("读取本次任务记忆") }, get query_memory() { return tr("查找相关记忆") },
  get batch_get_details() { return tr("批量读取详情") }, get batch_read_media() { return tr("批量查看素材") },
  get get_clip_readiness() { return tr("核对制作准备") }, get get_reference_candidates() { return tr("查找可用参考") },
  get get_set_dependencies() { return tr("核对片场承接") }, get configure_set_dependencies() { return tr("修订片场依赖") },
  get configure_state_preparation() { return tr("安排状态图来源") }, get confirm_set_handoff() { return tr("确认实拍承接") },
  get adopt_observed_reference() { return tr("复用实拍参考图") },
  get compare_candidates() { return tr("对比生成候选") }, get get_operation_receipt() { return tr("核对操作结果") },
  get patch_reference_plan() { return tr("调整参考清单") },
  get correct_memory_scope() { return tr("纠正记忆适用范围") }, get confirm_candidate_state() { return tr("确认实际末态") },
  get correct_state_scope() { return tr("纠正资产状态时段") },
  get record_state_evolution() { return tr("确认状态演变") },
  get state_evolution_review() { return tr("核对历史状态来源") },
  get save_reference_plan() { return tr("保存参考清单") }, get finalize_reference_plan() { return tr("核对参考与提示词") },
  get plan_story_events() { return tr("规划剧情事件") }, get update_clip_context() { return tr("更新片段状态计划") },
  get get_detail() { return tr("读取详情") }, get get_body() { return tr("读取完整内容") }, get commandExecution() { return tr("执行命令") }, get fileChange() { return tr("修改文件") }, get dynamicToolCall() { return tr("调用工具") },
  get save_screenplay() { return tr("更新剧本") }, get write_memory() { return tr("更新记忆") }, get create_asset() { return tr("建立视觉资产") },
  get create_state() { return tr("新增剧情状态") }, get generate_reference() { return tr("生成参考图") }, get prepare_reference() { return tr("准备参考素材") }, get select_reference() { return tr("选用参考图") },
  get prepare_sets() { return tr("规划片场") }, get plan_clips() { return tr("编排片段") }, get freeze_production() { return tr("开始制作") },
  get propose_global() { return tr("提交全局修改建议") }, get resolve_proposal() { return tr("处理全局建议") }, get generate_clip() { return tr("生成片段") },
  get analyze_candidate() { return tr("检查片段") }, get review_candidate() { return tr("评价片段") }, get repair_clip() { return tr("修复片段") },
  get assess_revision() { return tr("检查修订影响") }, get reconcile_job() { return tr("核实生成任务") }, get set_status() { return tr("更新进度") },
  get finish_set() { return tr("完成片场") }, get assemble() { return tr("合成成片") }, get finish_project() { return tr("完成项目") },
  get prepare_media() { return tr("截取参考素材") }, get review_reference() { return tr("检查参考图") }, get resolve_quality() { return tr("总导演处理质量问题") },
  get plan_voice() { return tr("安排人物音色") }, get establish_voice() { return tr("建立固定音色") },
  get select_voice() { return tr("选用音色版本") }, get approve_asset_observation() { return tr("确认资产画面细节") },
}
const fieldLabels: Record<string, string> = {
  get action() { return tr("操作") }, get scope() { return tr("记忆范围") }, get entries() { return tr("修改内容") }, get summary() { return tr("摘要") }, get text() { return tr("内容") },
  get set_id() { return tr("所属片场") }, get state_id() { return tr("剧情状态") }, get revision_id() { return tr("参考图修订") }, get view() { return tr("视图") },
  get reason() { return tr("修改原因") }, get prompt() { return tr("生成要求") }, get result() { return tr("结果") }, get arguments() { return tr("操作参数") }, get name() { return tr("工具") },
  get batch_assessments() { return tr("逐项检查判断") }, get finding_index() { return tr("分批问题序号") }, get conclusion() { return tr("判断") }, get media_ids() { return tr("查看的证据") },
}

export function DetailValue({ value }: { value: unknown }) {
  useLocale()

  if (typeof value === 'string') return <Markdown>{value}</Markdown>
  if (value === null || value === undefined) return <span>—</span>
  if (Array.isArray(value)) return <ul>{value.map((item, index) => <li key={index}><DetailValue value={item} /></li>)}</ul>
  if (typeof value === 'object') return <dl>{Object.entries(value).map(([key, item]) => <div key={key}><dt><EntityText>{fieldLabels[key] ?? key}</EntityText></dt><dd><DetailValue value={typeof item === 'string' && key === 'action' ? actionLabels[item] ?? item : key === 'scope' && item === 'global' ? tr("全局记忆") : item} /></dd></div>)}</dl>
  return <span>{String(value)}</span>
}
