import Link from 'next/link'
import {
  BookOpen,
  MessageSquare,
  Settings,
  CreditCard,
  RefreshCw,
  HelpCircle
} from 'lucide-react'
import type { Instance } from '@/hooks/useInstance'
import type { Subscription } from '@/hooks/useSubscription'

interface QuickActionsProps {
  instance: Instance | null
  subscription: Subscription | null
}

export function QuickActions({ instance, subscription }: QuickActionsProps) {
  const actions = [
    {
      name: 'Documentation',
      description: 'Learn how to use MindRoom',
      href: 'https://docs.mindroom.app',
      icon: BookOpen,
      external: true,
    },
    {
      name: 'Manage Subscription',
      description: `Current: ${subscription?.tier || 'Free'} plan`,
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
    <div className="bg-white rounded-lg p-6 shadow-sm">
      <h2 className="text-xl font-bold mb-4">Quick Actions</h2>

      <div className="space-y-3">
        {actions.map((action) => {
          const Icon = action.icon
          return action.external ? (
            <a
              key={action.name}
              href={action.href}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-start gap-3 p-3 rounded-lg hover:bg-gray-50 transition-colors"
            >
              <div className="flex-shrink-0">
                <div className="w-10 h-10 bg-orange-100 rounded-lg flex items-center justify-center">
                  <Icon className="w-5 h-5 text-orange-600" />
                </div>
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-900">{action.name}</p>
                <p className="text-sm text-gray-500">{action.description}</p>
              </div>
            </a>
          ) : (
            <Link
              key={action.name}
              href={action.href}
              className="flex items-start gap-3 p-3 rounded-lg hover:bg-gray-50 transition-colors"
            >
              <div className="flex-shrink-0">
                <div className="w-10 h-10 bg-orange-100 rounded-lg flex items-center justify-center">
                  <Icon className="w-5 h-5 text-orange-600" />
                </div>
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-gray-900">{action.name}</p>
                <p className="text-sm text-gray-500">{action.description}</p>
              </div>
            </Link>
          )
        })}

        {/* Restart Instance Button (if instance is failed or stopped) */}
        {instance && (instance.status === 'failed' || instance.status === 'error' || instance.status === 'stopped') && (
          <button
            className="flex items-start gap-3 p-3 rounded-lg hover:bg-gray-50 transition-colors w-full text-left"
          >
            <div className="flex-shrink-0">
              <div className="w-10 h-10 bg-red-100 rounded-lg flex items-center justify-center">
                <RefreshCw className="w-5 h-5 text-red-600" />
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-gray-900">Restart Instance</p>
              <p className="text-sm text-gray-500">Get your MindRoom back online</p>
            </div>
          </button>
        )}
      </div>

      {/* Usage Summary */}
      {subscription && (
        <div className="mt-6 pt-6 border-t">
          <h3 className="text-sm font-medium text-gray-900 mb-3">Plan Limits</h3>
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-gray-600">AI Agents</span>
              <span className="font-medium">{subscription.max_agents}</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-600">Messages/Day</span>
              <span className="font-medium">{subscription.max_messages_per_day.toLocaleString()}</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-600">Storage</span>
              <span className="font-medium">{subscription.max_storage_gb}GB</span>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
