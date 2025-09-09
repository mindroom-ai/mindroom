'use client'

import { Bell, Menu } from 'lucide-react'
import { useAuth } from '@/hooks/useAuth'
import { DarkModeToggle } from '@/components/DarkModeToggle'

interface HeaderProps {
  sidebarOpen: boolean
  setSidebarOpen: (open: boolean) => void
}

export function Header({ sidebarOpen, setSidebarOpen }: HeaderProps) {
  const { user } = useAuth()

  return (
    <header className="sticky top-0 z-40 flex h-16 shrink-0 items-center gap-x-4 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-4 shadow-sm sm:gap-x-6 sm:px-6 lg:px-8">
      <button
        type="button"
        className="-m-2.5 p-2.5 text-gray-700 lg:hidden"
        onClick={() => setSidebarOpen(!sidebarOpen)}
      >
        <span className="sr-only">Open sidebar</span>
        <Menu className="h-6 w-6" />
      </button>

      <div className="flex flex-1 gap-x-4 self-stretch lg:gap-x-6">
        <div className="flex flex-1" />
        <div className="flex items-center gap-x-4 lg:gap-x-6">
          <DarkModeToggle />

          <button
            type="button"
            className="-m-2.5 p-2.5 text-gray-400 hover:text-gray-500 dark:text-gray-500 dark:hover:text-gray-400"
          >
            <span className="sr-only">View notifications</span>
            <Bell className="h-6 w-6" />
          </button>

          <div className="hidden lg:block lg:h-6 lg:w-px lg:bg-gray-200 dark:bg-gray-700" />

          <div className="flex items-center gap-x-4">
            <div className="hidden sm:flex sm:flex-col sm:items-end">
              <p className="text-sm font-semibold leading-6 text-gray-900 dark:text-gray-100">
                {user?.email ?? 'Account'}
              </p>
              <p className="text-xs leading-5 text-gray-500 dark:text-gray-400">Free Plan</p>
            </div>
            <div className="h-8 w-8 rounded-full bg-orange-500 flex items-center justify-center text-white font-semibold">
              {(user?.email?.[0]?.toUpperCase() ?? '?')}
            </div>
          </div>
        </div>
      </div>
    </header>
  )
}
