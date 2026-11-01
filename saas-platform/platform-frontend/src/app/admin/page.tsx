'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader } from '@/components/ui/Card'
import { Users, CreditCard, Server, DollarSign, Activity } from 'lucide-react'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface HealthStatus {
  status: string
  supabase: boolean
  stripe: boolean
}

interface RecentActivity {
  created_at: string
  action: string
  account_id: string | null
}

interface AdminMetrics {
  total_accounts: number
  active_subscriptions: number
  instances_by_status: Record<string, number>
  subscription_revenue: number
  recent_instances: RecentActivity[]
}

function normalizeAdminMetrics(data: Partial<AdminMetrics> | null | undefined): AdminMetrics {
  const metrics = data ?? {}

  return {
    total_accounts: metrics.total_accounts ?? 0,
    active_subscriptions: metrics.active_subscriptions ?? 0,
    instances_by_status: metrics.instances_by_status ?? {},
    subscription_revenue: metrics.subscription_revenue ?? 0,
    recent_instances: metrics.recent_instances ?? [],
  }
}

// Shown in place of a figure the metrics request did not return.
const UNAVAILABLE = '—'

export default function AdminDashboard() {
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [metrics, setMetrics] = useState<AdminMetrics | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [healthRes, metricsRes] = await Promise.all([
          apiCall('/health'),
          apiCall('/admin/metrics/dashboard'),
        ])

        if (healthRes.ok) {
          const data = await healthRes.json() as HealthStatus
          setHealth(data)
        } else {
          setHealth({ status: 'degraded', supabase: false, stripe: false })
        }
        if (metricsRes.ok) {
          const data = await metricsRes.json() as Partial<AdminMetrics> | null
          setMetrics(normalizeAdminMetrics(data))
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

  const statCards = [
    {
      title: 'Total Accounts',
      value: metrics ? metrics.total_accounts : UNAVAILABLE,
      icon: Users,
    },
    {
      title: 'Active Subscriptions',
      value: metrics ? metrics.active_subscriptions : UNAVAILABLE,
      icon: CreditCard,
    },
    {
      title: 'Running Instances',
      value: metrics ? metrics.instances_by_status.running ?? 0 : UNAVAILABLE,
      icon: Server,
    },
    {
      title: 'MRR (est.)',
      value: metrics ? `$${metrics.subscription_revenue.toLocaleString()}` : UNAVAILABLE,
      icon: DollarSign,
    },
    {
      title: 'System Health',
      value: health?.status === 'ok' ? 'Operational' : 'Degraded',
      icon: Activity,
      detail: health ? `Supabase: ${health.supabase ? '✓ Healthy' : '✗ Error'} | Stripe: ${health.stripe ? '✓ Healthy' : '✗ Error'}` : 'Checking...',
      healthy: health?.status === 'ok',
    },
  ]
  const recentInstances = metrics?.recent_instances ?? []

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Admin Dashboard</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-2">System overview and key metrics</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
        {statCards.map((stat) => (
          <Card key={stat.title} className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 text-sm font-medium text-gray-700 dark:text-gray-300">
              {stat.title}
              <stat.icon className="h-4 w-4 text-gray-500 dark:text-gray-400" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{stat.value}</div>
              {stat.detail && (
                <p className={`text-xs mt-1 ${
                  stat.healthy ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
                }`}>
                  {stat.detail}
                </p>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Recent Activity */}
      <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
        <CardHeader className="text-gray-900 dark:text-gray-100">Recent Activity</CardHeader>
        <CardContent>
          <div className="space-y-4">
            {recentInstances.length ? (
              recentInstances.map((activity) => (
                <div
                  key={`${activity.created_at}-${activity.account_id ?? 'system'}-${activity.action}`}
                  className="flex items-center justify-between"
                >
                  <div>
                    <p className="text-sm font-medium">{activity.action}</p>
                    <p className="text-xs text-gray-500">
                      {activity.account_id ?? 'System'} - {activity.created_at}
                    </p>
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-gray-500 dark:text-gray-400">No recent activity</p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
