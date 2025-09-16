'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Users, CreditCard, Server, Activity } from 'lucide-react'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface AdminStats {
  // Keep legacy shape for compatibility; we compute values defensively
  accounts_count?: number
  subscriptions_count?: number
  instances_count?: number
}

interface HealthStatus {
  status: string
  supabase: boolean
  stripe: boolean
}

interface AdminMetrics {
  totalAccounts: number
  activeSubscriptions: number
  runningInstances: number
  mrr: number
}

export default function AdminDashboard() {
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [metrics, setMetrics] = useState<AdminMetrics | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchData = async () => {
      try {
        // Admin stats
        const [statsRes, healthRes, metricsRes] = await Promise.all([
          apiCall('/admin/stats'),
          apiCall('/health'),
          apiCall('/admin/metrics/dashboard'),
        ])

        if (statsRes.ok) {
          const data = await statsRes.json()
          setStats(data)
        }
        if (healthRes.ok) {
          const data = await healthRes.json()
          setHealth(data)
        } else {
          setHealth({ status: 'degraded', supabase: false, stripe: false })
        }
        if (metricsRes.ok) {
          const data = await metricsRes.json()
          setMetrics({
            totalAccounts: data.totalAccounts ?? 0,
            activeSubscriptions: data.activeSubscriptions ?? 0,
            runningInstances: data.runningInstances ?? 0,
            mrr: data.mrr ?? 0,
          })
        }
      } catch (error) {
        logger.error('Error fetching admin data:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-lg">Loading...</div>
      </div>
    )
  }

  const accountsVal = (stats as any)?.accounts ?? stats?.accounts_count ?? 0
  const subsVal = (stats as any)?.active_subscriptions ?? stats?.subscriptions_count ?? 0
  const runningVal = (stats as any)?.running_instances ?? stats?.instances_count ?? 0

  const statCards = [
    {
      title: 'Total Accounts',
      value: accountsVal,
      icon: Users,
      change: '+12%',
      changeType: 'positive' as const
    },
    {
      title: 'Active Subscriptions',
      value: subsVal,
      icon: CreditCard,
      change: '+8%',
      changeType: 'positive' as const
    },
    {
      title: 'Running Instances',
      value: runningVal,
      icon: Server,
      change: '+23%',
      changeType: 'positive' as const
    },
    {
      title: 'System Health',
      value: health?.status === 'ok' ? 'Operational' : 'Degraded',
      icon: Activity,
      change: health ? `Supabase: ${health.supabase ? '✓ Healthy' : '✗ Error'} | Stripe: ${health.stripe ? '✓ Healthy' : '✗ Error'}` : 'Checking...',
      changeType: (health && health.status === 'ok') ? 'positive' as const : 'negative' as const
    },
  ]

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Admin Dashboard</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-2">System overview and key metrics</p>
      </div>

      {/* API-backed Metrics */}
      {metrics && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
          <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">MRR (est.)</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">${metrics.mrr.toLocaleString()}</div>
            </CardContent>
          </Card>
          <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">Accounts</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{metrics.totalAccounts}</div>
            </CardContent>
          </Card>
          <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">Active Subs</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{metrics.activeSubscriptions}</div>
            </CardContent>
          </Card>
          <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">Running Inst.</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{metrics.runningInstances}</div>
            </CardContent>
          </Card>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {statCards.map((stat) => (
          <Card key={stat.title} className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {stat.title}
              </CardTitle>
              <stat.icon className="h-4 w-4 text-gray-500 dark:text-gray-400" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{stat.value}</div>
              <p className={`text-xs mt-1 ${
                stat.changeType === 'positive' ? 'text-green-600 dark:text-green-400' :
                stat.changeType === 'negative' ? 'text-red-600 dark:text-red-400' :
                'text-gray-600 dark:text-gray-400'
              }`}>
                {stat.change}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Recent Activity */}
      <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
        <CardHeader>
          <CardTitle className="text-gray-900 dark:text-gray-100">Recent Activity</CardTitle>
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
