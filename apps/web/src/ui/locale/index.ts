import { useSyncExternalStore } from 'react'
import english from './en.json'
import systemEnglish from './system.en.json'

export type Locale = 'zh-CN' | 'en'
export const localeNames: Record<Locale, string> = { 'zh-CN': '简体中文', en: 'English' }
const storageKey = 'stateweaver:locale'
const messages: Readonly<Record<string, string>> = english
const systemMessages: Readonly<Record<string, string>> = systemEnglish
const escapePattern = (text: string) => text.replace(/[.*+?^$(){}|[\]\\]/g, '\\$&')
const systemPatterns = Object.entries(systemMessages).filter(([key]) => key.includes('{{')).map(([key, value]) => {
  const parts = key.split(/\{\{([0-9]+)\}\}/)
  const captures = parts.filter((_, index) => index % 2 === 1).map(Number)
  const pattern = parts.map((part, index) => index % 2 ? '([\\s\\S]{1,320}?)' : escapePattern(part)).join('')
  return { pattern: new RegExp('^' + pattern + '$'), value, captures }
})
const listeners = new Set<() => void>()

function readLocale(): Locale {
  try { return localStorage.getItem(storageKey) === 'en' ? 'en' : 'zh-CN' }
  catch { return 'zh-CN' }
}

let locale = readLocale()

export function getLocale(): Locale { return locale }

function updateDocument() {
  if (typeof document === 'undefined') return
  document.documentElement.lang = locale
  document.title = locale === 'en' ? 'StateWeaver · Video Studio' : 'StateWeaver · 智能视频工作室'
}

export function setLocale(next: Locale) {
  if (locale === next) return
  locale = next
  try { localStorage.setItem(storageKey, next) }
  catch { /* The current tab can keep the choice in memory. */ }
  updateDocument()
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function useLocale(): Locale {
  return useSyncExternalStore(subscribe, getLocale, getLocale)
}

function interpolate(message: string, values: readonly unknown[]): string {
  return values.length
    ? message.replace(/\{\{([0-9]+)\}\}/g, (_, index: string) => String(values[Number(index)]))
    : message
}

export function tr(message: string, ...values: unknown[]): string {
  const translated = locale === 'en' && Object.hasOwn(messages, message) ? messages[message] : message
  return interpolate(translated, values)
}

/** Translate Studio diagnostics while retaining names, IDs and original provider details. */
export function trSystem(message: string): string {
  if (locale !== 'en') return message
  if (Object.hasOwn(systemMessages, message)) return systemMessages[message]
  if (Object.hasOwn(messages, message)) return messages[message]
  if (message.length > 4_096 || !/\p{Script=Han}/u.test(message)) return message
  for (const { pattern, value, captures } of systemPatterns) {
    const match = pattern.exec(message)
    if (!match) continue
    const values: string[] = []
    captures.forEach((index, offset) => { values[index] = match[offset + 1] })
    return interpolate(value, values)
  }
  return message
}

updateDocument()
