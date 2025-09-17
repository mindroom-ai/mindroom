'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/button'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface Subscription {
  id: string
  account_id: string
  price_tier: string
  tier: string
  status: string
  price: number
  billing_period: string
  current_period_end: string | null
  created_at: string
  accounts?: {
    email: string
    full_name: string | null
  }
}

export default function SubscriptionsPage() {
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchSubscriptions = async () => {
      try {
        const response = await apiCall('/admin/subscriptions')
        if (response.ok) {
          const data = await response.json()
          // Generic admin list endpoint returns { data, total }
          setSubscriptions(data.data || [])
        } else {
          logger.error('Failed to fetch subscriptions:', response.statusText)
        }
      } catch (error) {
        logger.error('Error fetching subscriptions:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchSubscriptions()
  }, [])

  const formatPrice = (amount: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
    }).format(amount / 100)
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-lg">Loading...</div>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-8 flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Subscriptions</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-2">Manage customer subscriptions and billing</p>
        </div>
        <Button onClick={() => alert('Export functionality coming soon')}>Export</Button>
      </div>

      <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
        <CardHeader>
          <CardTitle className="text-gray-900 dark:text-gray-100">All Subscriptions</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Customer</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Plan</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Status</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Price</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Started</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Next Bill</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Actions</th>
                </tr>
              </thead>
              <tbody>
                {subscriptions?.map((subscription) => (
                  <tr key={subscription.id} className="border-b border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">
                    <td className="py-3 px-4">
                      <div>
                        <div className="font-medium text-gray-900 dark:text-gray-100">
                          {subscription.accounts?.email || 'No email'}
                        </div>
                        <div className="text-sm text-gray-500 dark:text-gray-400">
                          {subscription.accounts?.full_name || '-'}
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <div className="font-medium capitalize text-gray-900 dark:text-gray-100">
                        {subscription.price_tier || subscription.tier || 'Unknown'}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        subscription.status === 'active' ? 'bg-green-100 dark:bg-green-900/20 text-green-800 dark:text-green-400' :
                        subscription.status === 'canceled' ? 'bg-red-100 dark:bg-red-900/20 text-red-800 dark:text-red-400' :
                        subscription.status === 'past_due' ? 'bg-yellow-100 dark:bg-yellow-900/20 text-yellow-800 dark:text-yellow-400' :
                        'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-400'
                      }`}>
                        {subscription.status}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-gray-900 dark:text-gray-100">
                        {formatPrice(subscription.price || 0)}
                      </span>
                      <span className="text-gray-500 dark:text-gray-400 text-sm">
                        /{subscription.billing_period || 'month'}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                      {new Date(subscription.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                      {subscription.current_period_end
                        ? new Date(subscription.current_period_end).toLocaleDateString()
                        : '-'
                      }
                    </td>
                    <td className="py-3 px-4">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300"
                        onClick={() => alert(`Managing subscription ${subscription.id}`)}
                      >
                        Manage
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {(!subscriptions || subscriptions.length === 0) && (
              <div className="text-center py-8 text-gray-500">
                No subscriptions found
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
