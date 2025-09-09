'use client'

import { createContext, useContext, useEffect, useState } from 'react'

type DarkModeContextType = {
  isDarkMode: boolean
  mode: 'light' | 'dark' | 'system'
  setMode: (mode: 'light' | 'dark' | 'system') => void
}

const DarkModeContext = createContext<DarkModeContextType | undefined>(undefined)

export function DarkModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setMode] = useState<'light' | 'dark' | 'system'>('system')
  const [isDarkMode, setIsDarkMode] = useState(false)

  useEffect(() => {
    // Get saved preference or default to system
    const savedMode = localStorage.getItem('darkMode') as 'light' | 'dark' | 'system' | null
    if (savedMode) {
      setMode(savedMode)
    }
  }, [])

  useEffect(() => {
    const updateDarkMode = () => {
      let shouldBeDark = false

      if (mode === 'dark') {
        shouldBeDark = true
      } else if (mode === 'light') {
        shouldBeDark = false
      } else {
        // System mode - check system preference
        shouldBeDark = window.matchMedia('(prefers-color-scheme: dark)').matches
      }

      setIsDarkMode(shouldBeDark)

      // Apply/remove dark class on html element
      if (shouldBeDark) {
        document.documentElement.classList.add('dark')
      } else {
        document.documentElement.classList.remove('dark')
      }
    }

    updateDarkMode()

    // Listen for system theme changes when in system mode
    if (mode === 'system') {
      const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
      mediaQuery.addEventListener('change', updateDarkMode)
      return () => mediaQuery.removeEventListener('change', updateDarkMode)
    }
  }, [mode])

  const handleSetMode = (newMode: 'light' | 'dark' | 'system') => {
    setMode(newMode)
    localStorage.setItem('darkMode', newMode)
  }

  return (
    <DarkModeContext.Provider value={{ isDarkMode, mode, setMode: handleSetMode }}>
      {children}
    </DarkModeContext.Provider>
  )
}

export function useDarkMode() {
  const context = useContext(DarkModeContext)
  if (context === undefined) {
    throw new Error('useDarkMode must be used within a DarkModeProvider')
  }
  return context
}
