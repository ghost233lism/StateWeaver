import { useLocale } from "./locale"
import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import './theme.css'

export type Theme = 'light' | 'dark'

export const DEFAULT_THEME_STORAGE_KEY = 'agentvideo:ui-theme:v2'
export const FONT_SCALE_STORAGE_KEY = 'agentvideo:ui-font-scale:v1'
export const DEFAULT_FONT_SCALE = 100
export const MIN_FONT_SCALE = 90
export const MAX_FONT_SCALE = 130
export const FONT_SCALE_STEP = 5

type ThemeStorage = Pick<Storage, 'getItem' | 'setItem'>

interface ThemeContextValue {
  theme: Theme
  setTheme: (theme: Theme) => void
  fontScale: number
  setFontScale: (scale: number) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

function browserStorage(): ThemeStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function storedTheme(storage: ThemeStorage | null, storageKey: string, fallback: Theme): Theme {
  try {
    const value = storage?.getItem(storageKey)
    return value === 'light' || value === 'dark' ? value : fallback
  } catch {
    return fallback
  }
}

function storedFontScale(storage: ThemeStorage | null): number {
  try {
    const value = Number(storage?.getItem(FONT_SCALE_STORAGE_KEY))
    return Number.isFinite(value) && value >= MIN_FONT_SCALE && value <= MAX_FONT_SCALE && value % FONT_SCALE_STEP === 0
      ? value : DEFAULT_FONT_SCALE
  } catch {
    return DEFAULT_FONT_SCALE
  }
}

export interface ThemeProviderProps {
  children: ReactNode
  defaultTheme?: Theme
  storage?: ThemeStorage | null
  storageKey?: string
}

export function ThemeProvider({
  children,
  defaultTheme = 'light',
  storage,
  storageKey = DEFAULT_THEME_STORAGE_KEY,
}: ThemeProviderProps) {
  useLocale()

  const resolvedStorage = storage === undefined ? browserStorage() : storage
  const [theme, setThemeState] = useState<Theme>(() => storedTheme(resolvedStorage, storageKey, defaultTheme))
  const [fontScale, setFontScaleState] = useState(() => storedFontScale(resolvedStorage))

  const setTheme = useCallback((nextTheme: Theme) => {
    setThemeState(nextTheme)
  }, [])

  const setFontScale = useCallback((scale: number) => {
    if (!Number.isFinite(scale)) return
    setFontScaleState(Math.max(MIN_FONT_SCALE, Math.min(MAX_FONT_SCALE, Math.round(scale / FONT_SCALE_STEP) * FONT_SCALE_STEP)))
  }, [])

  useLayoutEffect(() => {
    const root = document.documentElement
    root.dataset.stateweaverTheme = theme
    root.style.colorScheme = theme
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#151922' : '#f3f5f8')
    try {
      resolvedStorage?.setItem(storageKey, theme)
    } catch {
      // Appearance still works when browser storage is unavailable.
    }
  }, [resolvedStorage, storageKey, theme])

  useLayoutEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--av-font-scale', String(fontScale / 100))
    root.dataset.stateweaverFontScale = String(fontScale)
    try {
      resolvedStorage?.setItem(FONT_SCALE_STORAGE_KEY, String(fontScale))
    } catch {
      // Font controls remain usable without persistent browser storage.
    }
  }, [fontScale, resolvedStorage])

  const value = useMemo(() => ({ theme, setTheme, fontScale, setFontScale }), [setTheme, theme, fontScale, setFontScale])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme must be used inside ThemeProvider')
  return context
}
