import { localeNames, setLocale, tr, useLocale, type Locale } from '../../ui/locale'
import { useEffect, useId, useRef, useState } from 'react'
import { Minus, Moon, Plus, SlidersHorizontal, Sun, X } from 'lucide-react'
import { DEFAULT_FONT_SCALE, FONT_SCALE_STEP, MAX_FONT_SCALE, MIN_FONT_SCALE, useTheme } from '../../ui/ThemeProvider'
import './AppearanceSettings.css'

export function AppearanceSettings({ productionLanguage }: { productionLanguage?: Locale } = {}) {
  const language = useLocale()

  const { theme, setTheme, fontScale, setFontScale } = useTheme()
  const [open, setOpen] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const id = useId()
  const close = () => { setOpen(false); trigger.current?.focus({ preventScroll: true }) }

  useEffect(() => {
    if (!open) return
    panel.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus({ preventScroll: true })
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !container.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  return <div className="studio-appearance" ref={container} onBlur={(event) => {
    if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) setOpen(false)
  }} onKeyDown={(event) => {
    if (open && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() }
  }}>
    <button ref={trigger} className="studio-button studio-button--quiet studio-appearance-trigger" type="button" aria-label={tr("显示设置")} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} title={tr("语言、主题和字号")} onClick={() => setOpen((value) => !value)}>
      <SlidersHorizontal size={18} aria-hidden="true" /><span>{tr("显示设置")}</span>
    </button>
    {open && <div ref={panel} id={id} className="studio-appearance-panel" role="dialog" aria-labelledby={`${id}-title`}>
      <header><h2 id={`${id}-title`}>{tr("显示设置")}</h2><button className="studio-icon" type="button" aria-label={tr("关闭显示设置")} onClick={close}><X size={18} aria-hidden="true" /></button></header>
      <fieldset><legend>{tr("界面主题")}</legend><div className="studio-appearance-themes">
        <button type="button" aria-label={tr("使用浅色主题")} aria-pressed={theme === 'light'} onClick={() => setTheme('light')}><Sun size={18} aria-hidden="true" />{tr("浅色")}</button>
        <button type="button" aria-label={tr("使用深色主题")} aria-pressed={theme === 'dark'} onClick={() => setTheme('dark')}><Moon size={18} aria-hidden="true" />{tr("深色")}</button>
      </div></fieldset>
      <fieldset>
        <legend>{tr('界面与新故事语言')}</legend>
        <div className="studio-appearance-themes">
          {(['zh-CN', 'en'] as const).map((option) => <button
            key={option} type="button" lang={option} aria-pressed={language === option}
            onClick={() => setLocale(option)}
          >{localeNames[option]}</button>)}
        </div>
        {productionLanguage && <p className="studio-appearance-language">
          {tr('本项目的制作语言 · {{0}}', localeNames[productionLanguage])}
        </p>}
      </fieldset>
      <div className="studio-appearance-size">
        <div className="studio-appearance-size-heading"><label htmlFor={`${id}-size`}>{tr("字号大小")}</label><output htmlFor={`${id}-size`} aria-label={tr("当前字号")} aria-live="polite">{fontScale}%</output></div>
        <div className="studio-appearance-range">
          <button className="studio-icon" type="button" aria-label={tr("减小字号")} disabled={fontScale <= MIN_FONT_SCALE} onClick={() => setFontScale(fontScale - FONT_SCALE_STEP)}><Minus size={17} aria-hidden="true" /></button>
          <input id={`${id}-size`} type="range" min={MIN_FONT_SCALE} max={MAX_FONT_SCALE} step={FONT_SCALE_STEP} value={fontScale} aria-valuetext={`${fontScale}%${fontScale === DEFAULT_FONT_SCALE ? tr("，默认字号") : ''}`} onChange={(event) => setFontScale(Number(event.target.value))} />
          <button className="studio-icon" type="button" aria-label={tr("增大字号")} disabled={fontScale >= MAX_FONT_SCALE} onClick={() => setFontScale(fontScale + FONT_SCALE_STEP)}><Plus size={17} aria-hidden="true" /></button>
        </div>
        <div className="studio-appearance-size-footer"><span>{MIN_FONT_SCALE}%–{MAX_FONT_SCALE}%</span><button className="studio-text-button" type="button" disabled={fontScale === DEFAULT_FONT_SCALE} onClick={() => setFontScale(DEFAULT_FONT_SCALE)}>{tr("恢复默认字号")}</button></div>
      </div>
      <p className="studio-appearance-preview">{tr("画布、记忆与导演输出同步调整。")}</p>
      <footer>{tr("设置自动保存在当前浏览器")}</footer>
    </div>}
  </div>
}
