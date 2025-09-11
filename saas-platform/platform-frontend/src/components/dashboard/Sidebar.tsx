'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useState } from 'react'
import {
  Home,
  Server,
  CreditCard,
  BarChart3,
  Settings,
  HelpCircle,
  LogOut,
  X,
  Loader2
} from 'lucide-react'
import { useAuth } from '@/hooks/useAuth'
import { MindRoomLogo } from '@/components/MindRoomLogo'

const navigation = [
  { name: 'Dashboard', href: '/dashboard', icon: Home },
  { name: 'Instance', href: '/dashboard/instance', icon: Server },
  { name: 'Billing', href: '/dashboard/billing', icon: CreditCard },
  { name: 'Usage', href: '/dashboard/usage', icon: BarChart3 },
  { name: 'Settings', href: '/dashboard/settings', icon: Settings },
  { name: 'Support', href: '/dashboard/support', icon: HelpCircle },
]

interface SidebarProps {
  sidebarOpen: boolean
  setSidebarOpen: (open: boolean) => void
}

export function Sidebar({ sidebarOpen, setSidebarOpen }: SidebarProps) {
  const pathname = usePathname()
  const { signOut } = useAuth()
  const [isSigningOut, setIsSigningOut] = useState(false)

  // Extract common navigation rendering with dark mode support
  const renderNavigation = (onLinkClick?: () => void) => (
    <>
      {navigation.map((item) => {
        const Icon = item.icon
        const isActive = pathname === item.href
        return (
          <li key={item.name}>
            <Link
              href={item.href}
              onClick={onLinkClick}
              className={`
                group flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold transition-all duration-150
                ${isActive
                  ? 'bg-orange-50 dark:bg-orange-900/20 text-orange-600 dark:text-orange-400'
                  : 'text-gray-700 dark:text-gray-300 hover:text-orange-600 dark:hover:text-orange-400 hover:bg-gray-50 dark:hover:bg-gray-800'
                }
                active:scale-95 active:bg-orange-100 dark:active:bg-orange-900/30
              `}
            >
              <Icon className={`h-6 w-6 shrink-0 ${isActive ? 'text-orange-600 dark:text-orange-400' : 'text-gray-400 dark:text-gray-500 group-hover:text-orange-600 dark:group-hover:text-orange-400'}`} />
              {item.name}
            </Link>
          </li>
        )
      })}
    </>
  )

  // Extract common logo with dark mode support
  const logo = (
    <Link href="/dashboard" className="flex items-center gap-2">
      <MindRoomLogo className="text-orange-500" size={32} />
      <span className="text-xl font-bold dark:text-white">MindRoom</span>
    </Link>
  )

  // Extract sign out button with dark mode support
  const handleSignOut = async () => {
    setIsSigningOut(true)
    try {
      await signOut()
    } finally {
      setIsSigningOut(false)
    }
  }

  const signOutButton = (
    <button
      onClick={handleSignOut}
      disabled={isSigningOut}
      className="group -mx-2 flex gap-x-3 rounded-md p-2 text-sm font-semibold leading-6 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-orange-600 dark:hover:text-orange-400 w-full transition-all duration-150 active:scale-95 active:bg-orange-100 dark:active:bg-orange-900/30 disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {isSigningOut ? (
        <Loader2 className="h-6 w-6 shrink-0 text-gray-400 dark:text-gray-500 animate-spin" />
      ) : (
        <LogOut className="h-6 w-6 shrink-0 text-gray-400 dark:text-gray-500 group-hover:text-orange-600 dark:group-hover:text-orange-400" />
      )}
      <span className="select-none">{isSigningOut ? 'Signing out...' : 'Sign out'}</span>
    </button>
  )

  return (
    <>
      {/* Mobile sidebar overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-gray-900 bg-opacity-75 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Mobile sidebar */}
      <div className={`
        fixed inset-y-0 left-0 z-50 w-72 transform transition-transform duration-300 ease-in-out lg:hidden
        ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
      `}>
        <div className="flex h-full flex-col gap-y-5 overflow-y-auto border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-6 pb-4">
          <div className="flex h-16 shrink-0 items-center justify-between">
            {logo}
            <button
              type="button"
              className="-m-2.5 p-2.5 text-gray-700 dark:text-gray-300 hover:text-orange-600 dark:hover:text-orange-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors"
              onClick={() => setSidebarOpen(false)}
            >
              <span className="sr-only">Close sidebar</span>
              <X className="h-6 w-6" />
            </button>
          </div>
          <nav className="flex flex-1 flex-col">
            <ul role="list" className="flex flex-1 flex-col gap-y-7">
              <li>
                <ul role="list" className="-mx-2 space-y-1">
                  {renderNavigation(() => setSidebarOpen(false))}
                </ul>
              </li>
              <li className="mt-auto">
                {signOutButton}
              </li>
            </ul>
          </nav>
        </div>
      </div>

      {/* Desktop sidebar */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:z-50 lg:flex lg:w-72 lg:flex-col">
        <div className="flex grow flex-col gap-y-5 overflow-y-auto border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-6 pb-4">
          <div className="flex h-16 shrink-0 items-center">
            {logo}
          </div>
          <nav className="flex flex-1 flex-col">
            <ul role="list" className="flex flex-1 flex-col gap-y-7">
              <li>
                <ul role="list" className="-mx-2 space-y-1">
                  {renderNavigation()}
                </ul>
              </li>
              <li className="mt-auto">
                {signOutButton}
              </li>
            </ul>
          </nav>
        </div>
      </div>
    </>
  )
}
