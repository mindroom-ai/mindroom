'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import {
  Users,
  CreditCard,
  Server,
  FileText,
  BarChart3,
  Home,
  LogOut
} from 'lucide-react'

const navItems = [
  { name: 'Dashboard', href: '/admin', icon: Home },
  { name: 'Accounts', href: '/admin/accounts', icon: Users },
  { name: 'Subscriptions', href: '/admin/subscriptions', icon: CreditCard },
  { name: 'Instances', href: '/admin/instances', icon: Server },
  { name: 'Audit Logs', href: '/admin/audit-logs', icon: FileText },
  { name: 'Usage Metrics', href: '/admin/usage', icon: BarChart3 },
]

export function AdminLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()

  return (
    <div className="flex h-screen bg-gray-50 dark:bg-gray-950">
      {/* Sidebar */}
      <div className="w-64 bg-white dark:bg-gray-900 shadow-lg">
        <div className="p-6">
          <h1 className="text-2xl font-bold text-gray-800 dark:text-gray-100">MindRoom Admin</h1>
        </div>

        <nav className="mt-6">
          {navItems.map((item) => {
            const Icon = item.icon
            const isActive = pathname === item.href ||
                           (item.href !== '/admin' && pathname.startsWith(item.href))

            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex items-center px-6 py-3 text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-orange-50 dark:bg-orange-900/20 text-orange-600 dark:text-orange-400 border-r-4 border-orange-600 dark:border-orange-400'
                    : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-gray-100'
                }`}
              >
                <Icon className="w-5 h-5 mr-3" />
                {item.name}
              </Link>
            )
          })}
        </nav>

        <div className="absolute bottom-0 w-64 p-6">
          <Link
            href="/dashboard"
            className="flex items-center text-sm text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100"
          >
            <LogOut className="w-4 h-4 mr-2" />
            Exit Admin
          </Link>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-auto bg-gray-50 dark:bg-gray-950">
        <div className="p-8">
          {children}
        </div>
      </div>
    </div>
  )
}
