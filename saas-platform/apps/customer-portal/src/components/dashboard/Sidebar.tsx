'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import {
  Home,
  Server,
  CreditCard,
  BarChart3,
  Settings,
  HelpCircle,
  LogOut
} from 'lucide-react'
import { useAuth } from '@/hooks/useAuth'

const navigation = [
  { name: 'Dashboard', href: '/dashboard', icon: Home },
  { name: 'Instance', href: '/dashboard/instance', icon: Server },
  { name: 'Billing', href: '/dashboard/billing', icon: CreditCard },
  { name: 'Usage', href: '/dashboard/usage', icon: BarChart3 },
  { name: 'Settings', href: '/dashboard/settings', icon: Settings },
  { name: 'Support', href: '/dashboard/support', icon: HelpCircle },
]

export function Sidebar() {
  const pathname = usePathname()
  const { signOut } = useAuth()

  return (
    <div className="hidden lg:fixed lg:inset-y-0 lg:z-50 lg:flex lg:w-72 lg:flex-col">
      <div className="flex grow flex-col gap-y-5 overflow-y-auto border-r border-gray-200 bg-white px-6 pb-4">
        <div className="flex h-16 shrink-0 items-center">
          <Link href="/dashboard" className="flex items-center gap-2">
            <span className="text-3xl">🧠</span>
            <span className="text-xl font-bold">MindRoom</span>
          </Link>
        </div>
        <nav className="flex flex-1 flex-col">
          <ul role="list" className="flex flex-1 flex-col gap-y-7">
            <li>
              <ul role="list" className="-mx-2 space-y-1">
                {navigation.map((item) => {
                  const Icon = item.icon
                  const isActive = pathname === item.href
                  return (
                    <li key={item.name}>
                      <Link
                        href={item.href}
                        className={`
                          group flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold
                          ${isActive
                            ? 'bg-orange-50 text-orange-600'
                            : 'text-gray-700 hover:text-orange-600 hover:bg-gray-50'
                          }
                        `}
                      >
                        <Icon className={`h-6 w-6 shrink-0 ${isActive ? 'text-orange-600' : 'text-gray-400 group-hover:text-orange-600'}`} />
                        {item.name}
                      </Link>
                    </li>
                  )
                })}
              </ul>
            </li>
            <li className="mt-auto">
              <button
                onClick={signOut}
                className="group -mx-2 flex gap-x-3 rounded-md p-2 text-sm font-semibold leading-6 text-gray-700 hover:bg-gray-50 hover:text-orange-600 w-full"
              >
                <LogOut className="h-6 w-6 shrink-0 text-gray-400 group-hover:text-orange-600" />
                Sign out
              </button>
            </li>
          </ul>
        </nav>
      </div>
    </div>
  )
}
