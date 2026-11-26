import { tr, useLocale } from '../../ui/locale'
const sections: Record<string, string> = {
  get subject_definitions() { return tr("主体与参考定义") }, get summary() { return tr("生成任务") }, get retention_analysis() { return tr("保留与接续要求") },
  get detailed_description() { return tr("画面与动作") }, get overall_soundscape() { return tr("环境与声音") }, get non_diegetic_music() { return tr("配乐") },
}

export function SixSectionPrompt({ prompt }: { prompt: string }) {
  useLocale()

  const matches = [...prompt.matchAll(/^(subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music):[ \t]*\n?/gm)]
  return <section className="studio-six-prompt" aria-label={tr("本次提交的六段式提示词")}><h3>{tr("六段式提示词")}</h3>
    {matches.length === 6 ? matches.map((match, index) => <details key={match[1]} open={match[1] === 'detailed_description'}><summary><span>{String(index + 1).padStart(2, '0')}</span>{sections[match[1]]}<small>{match[1]}</small></summary><pre>{prompt.slice(match.index! + match[0].length, matches[index + 1]?.index)}</pre></details>) : <pre>{prompt || tr("本次提示词尚未记录。")}</pre>}
    <details><summary>{tr("完整提交原文")}</summary><pre>{prompt}</pre></details>
  </section>
}
