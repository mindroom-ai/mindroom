'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { apiCall } from '@/lib/api'

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
          setSubscriptions(data.subscriptions || [])
        } else {
          console.error('Failed to fetch subscriptions:', response.statusText)
        }
      } catch (error) {
        console.error('Error fetching subscriptions:', error)
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
          <h1 className="text-3xl font-bold text-gray-900">Subscriptions</h1>
          <p className="text-gray-600 mt-2">Manage customer subscriptions and billing</p>
        </div>
        <Button>Export</Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>All Subscriptions</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-3 px-4">Customer</th>
                  <th className="text-left py-3 px-4">Plan</th>
                  <th className="text-left py-3 px-4">Status</th>
                  <th className="text-left py-3 px-4">Price</th>
                  <th className="text-left py-3 px-4">Started</th>
                  <th className="text-left py-3 px-4">Next Bill</th>
                  <th className="text-left py-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {subscriptions?.map((subscription) => (
                  <tr key={subscription.id} className="border-b hover:bg-gray-50">
                    <td className="py-3 px-4">
                      <div>
                        <div className="font-medium">
                          {subscription.accounts?.email}
                        </div>
                        <div className="text-sm text-gray-500">
                          {subscription.accounts?.full_name}
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <div className="font-medium capitalize">
                        {subscription.price_tier}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        subscription.status === 'active' ? 'bg-green-100 text-green-800' :
                        subscription.status === 'canceled' ? 'bg-red-100 text-red-800' :
                        subscription.status === 'past_due' ? 'bg-yellow-100 text-yellow-800' :
                        'bg-gray-100 text-gray-800'
                      }`}>
                        {subscription.status}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      {formatPrice(subscription.price || 0)}
                      <span className="text-gray-500 text-sm">
                        /{subscription.billing_period}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500">
                      {new Date(subscription.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500">
                      {subscription.current_period_end
                        ? new Date(subscription.current_period_end).toLocaleDateString()
                        : '-'
                      }
                    </td>
                    <td className="py-3 px-4">
                      <button className="text-blue-600 hover:text-blue-900 text-sm">
                        Manage
                      </button>
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
