import Link from 'next/link'
import {
  BookOpen,
  Settings,
  CreditCard,
  HelpCircle
} from 'lucide-react'
import type { Subscription } from '@/hooks/useSubscription'
import { Card, CardHeader } from '@/components/ui/Card'
import { planState } from '@/lib/plan-state'

interface QuickActionsProps {
  subscription: Subscription | null
}

export function QuickActions({ subscription }: QuickActionsProps) {
  const state = subscription ? planState(subscription) : null
  const actions = [
    {
      name: 'Documentation',
      description: 'Learn how to use MindRoom',
      href: 'https://docs.mindroom.chat/',
      icon: BookOpen,
      external: true,
    },
    {
      name: 'Manage Subscription',
      description: state === null
        ? 'Your plan and billing'
        : state === 'none'
          ? 'Choose a plan to run a hosted instance'
          : state === 'lapsed'
            ? 'Restore billing to run your instance'
            : 'Your plan and billing',
      href: '/dashboard/billing',
      icon: CreditCard,
      external: false,
    },
    {
      name: 'Configure Instance',
      description: 'Update settings and integrations',
      href: '/dashboard/instance',
      icon: Settings,
      external: false,
    },
    {
      name: 'Get Support',
      description: 'Contact our support team',
      href: '/dashboard/support',
      icon: HelpCircle,
      external: false,
    },
  ]

  return (
    <Card>
      <CardHeader className="mb-4">Quick Actions</CardHeader>

      <div className="space-y-3">
        {actions.map((action) => {
          const Icon = action.icon
          return action.external ? (
            <a
              key={action.name}
              href={action.href}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-start gap-3 p-3 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
            >
              <div className="flex-shrink-0">
                <div className="w-10 h-10 bg-orange-100 dark:bg-orange-900/30 rounded-lg flex items-center justify-center">
                  <Icon className="w-5 h-5 text-orange-600 dark:text-orange-400" />
                </div>
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{action.name}</p>
                <p className="text-sm text-gray-500 dark:text-gray-400">{action.description}</p>
              </div>
            </a>
          ) : (
            <Link
              key={action.name}
              href={action.href}
              className="flex items-start gap-3 p-3 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
            >
              <div className="flex-shrink-0">
                <div className="w-10 h-10 bg-orange-100 dark:bg-orange-900/30 rounded-lg flex items-center justify-center">
                  <Icon className="w-5 h-5 text-orange-600 dark:text-orange-400" />
                </div>
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{action.name}</p>
                <p className="text-sm text-gray-500 dark:text-gray-400">{action.description}</p>
              </div>
            </Link>
          )
        })}

      </div>

    </Card>
  )
}
