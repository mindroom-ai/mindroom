'use client'

import { Moon, Sun, Monitor } from 'lucide-react'
import { useDarkMode } from '@/hooks/useDarkMode'
import { useState, useRef, useEffect } from 'react'

export function DarkModeToggle() {
  const { isDarkMode, mode, setMode } = useDarkMode()
  const [showDropdown, setShowDropdown] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setShowDropdown(false)
      }
    }

    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        type="button"
        className="p-2 text-gray-400 hover:text-gray-500 dark:text-gray-500 dark:hover:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors"
        onClick={() => setShowDropdown(!showDropdown)}
        aria-label="Toggle dark mode"
      >
        {isDarkMode ? (
          <Moon className="h-5 w-5" />
        ) : (
          <Sun className="h-5 w-5" />
        )}
      </button>

      {showDropdown && (
        <div className="absolute right-0 mt-2 w-36 rounded-md bg-white dark:bg-gray-800 shadow-lg ring-1 ring-black ring-opacity-5 z-50">
          <div className="py-1">
            <button
              onClick={() => {
                setMode('light')
                setShowDropdown(false)
              }}
              className={`flex items-center px-4 py-2 text-sm w-full hover:bg-gray-100 dark:hover:bg-gray-700 ${
                mode === 'light' ? 'text-orange-600 dark:text-orange-400' : 'text-gray-700 dark:text-gray-300'
              }`}
            >
              <Sun className="h-4 w-4 mr-2" />
              Light
            </button>
            <button
              onClick={() => {
                setMode('dark')
                setShowDropdown(false)
              }}
              className={`flex items-center px-4 py-2 text-sm w-full hover:bg-gray-100 dark:hover:bg-gray-700 ${
                mode === 'dark' ? 'text-orange-600 dark:text-orange-400' : 'text-gray-700 dark:text-gray-300'
              }`}
            >
              <Moon className="h-4 w-4 mr-2" />
              Dark
            </button>
            <button
              onClick={() => {
                setMode('system')
                setShowDropdown(false)
              }}
              className={`flex items-center px-4 py-2 text-sm w-full hover:bg-gray-100 dark:hover:bg-gray-700 ${
                mode === 'system' ? 'text-orange-600 dark:text-orange-400' : 'text-gray-700 dark:text-gray-300'
              }`}
            >
              <Monitor className="h-4 w-4 mr-2" />
              System
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
