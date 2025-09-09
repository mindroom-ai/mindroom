'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Users, CreditCard, Server, Activity } from 'lucide-react'
import { apiCall } from '@/lib/api'

interface AdminStats {
  accounts_count: number
  subscriptions_count: number
  instances_count: number
  recent_activity: Array<{
    type: string
    description: string
    timestamp: string
  }>
}

export default function AdminDashboard() {
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const response = await apiCall('/api/admin/stats')
        if (response.ok) {
          const data = await response.json()
          setStats(data)
        } else {
          console.error('Failed to fetch admin stats:', response.statusText)
        }
      } catch (error) {
        console.error('Error fetching admin stats:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchStats()
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-lg">Loading...</div>
      </div>
    )
  }

  const statCards = [
    {
      title: 'Total Accounts',
      value: stats?.accounts_count || 0,
      icon: Users,
      change: '+12%',
      changeType: 'positive' as const
    },
    {
      title: 'Active Subscriptions',
      value: stats?.subscriptions_count || 0,
      icon: CreditCard,
      change: '+8%',
      changeType: 'positive' as const
    },
    {
      title: 'Running Instances',
      value: stats?.instances_count || 0,
      icon: Server,
      change: '+23%',
      changeType: 'positive' as const
    },
    {
      title: 'System Health',
      value: '99.9%',
      icon: Activity,
      change: 'Operational',
      changeType: 'neutral' as const
    },
  ]

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900">Admin Dashboard</h1>
        <p className="text-gray-600 mt-2">System overview and key metrics</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        {statCards.map((stat) => (
          <Card key={stat.title}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">
                {stat.title}
              </CardTitle>
              <stat.icon className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{stat.value}</div>
              <p className={`text-xs ${
                stat.changeType === 'positive' ? 'text-green-600' :
                stat.changeType === 'negative' ? 'text-red-600' :
                'text-gray-600'
              }`}>
                {stat.change}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Recent Activity */}
      <Card>
        <CardHeader>
          <CardTitle>Recent Activity</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {stats?.recent_activity?.map((activity, index) => (
              <div key={index} className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{activity.type}</p>
                  <p className="text-xs text-gray-500">{activity.description} - {activity.timestamp}</p>
                </div>
              </div>
            )) || (
              <>
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium">New account registered</p>
                    <p className="text-xs text-gray-500">user@example.com - 2 minutes ago</p>
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium">Instance deployed</p>
                    <p className="text-xs text-gray-500">customer-123 - 15 minutes ago</p>
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium">Subscription upgraded</p>
                    <p className="text-xs text-gray-500">Pro plan - 1 hour ago</p>
                  </div>
                </div>
              </>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
