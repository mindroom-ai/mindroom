'use client'

import { useAuth } from '@/hooks/useAuth'
import { useInstance } from '@/hooks/useInstance'
import { useSubscription } from '@/hooks/useSubscription'
import { InstanceCard } from '@/components/dashboard/InstanceCard'
import { UsageChart } from '@/components/dashboard/UsageChart'
import { QuickActions } from '@/components/dashboard/QuickActions'
import { Loader2 } from 'lucide-react'

export default function DashboardPage() {
  const { user } = useAuth()
  const { instance, loading: instanceLoading } = useInstance()
  const { subscription } = useSubscription()

  if (instanceLoading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 className="w-8 h-8 animate-spin text-orange-500" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Welcome Header */}
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <h1 className="text-2xl font-bold">Welcome back!</h1>
        <p className="text-gray-600 mt-1">
          Your MindRoom is {instance?.status === 'running' ? 'up and running' : instance?.status === 'provisioning' ? 'starting up' : 'currently offline'}
        </p>
      </div>

      {/* Instance Status and Quick Actions */}
      <div className="grid md:grid-cols-2 gap-6">
        <InstanceCard instance={instance} />
        <QuickActions instance={instance} subscription={subscription} />
      </div>

      {/* Usage Overview */}
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4">Usage This Month</h2>
        <UsageChart subscription={subscription} />
      </div>
    </div>
  )
}
