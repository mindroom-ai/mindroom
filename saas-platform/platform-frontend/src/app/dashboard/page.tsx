'use client'

import { useAuth } from '@/hooks/useAuth'
import { useInstance } from '@/hooks/useInstance'
import { useSubscription } from '@/hooks/useSubscription'
import { InstanceCard } from '@/components/dashboard/InstanceCard'
import { UsageChart } from '@/components/dashboard/UsageChart'
import { QuickActions } from '@/components/dashboard/QuickActions'
import { Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { setupAccount } from '@/lib/api'

export default function DashboardPage() {
  const { user } = useAuth()
  const { instance, loading: instanceLoading } = useInstance()
  const { subscription, loading: subscriptionLoading } = useSubscription()
  const [isSettingUp, setIsSettingUp] = useState(false)
  const router = useRouter()

  useEffect(() => {
    // Auto-setup free tier if user has no subscription
    const setupFreeTier = async () => {
      if (!user || subscriptionLoading || subscription || isSettingUp) {
        return
      }

      setIsSettingUp(true)
      try {
        await setupAccount()
        router.refresh()
      } catch (error) {
        console.error('Error setting up free tier:', error)
      } finally {
        setIsSettingUp(false)
      }
    }

    setupFreeTier()
  }, [user, subscriptionLoading, subscription, isSettingUp])

  if (instanceLoading || subscriptionLoading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <Loader2 className="w-8 h-8 animate-spin text-orange-500 mx-auto mb-4" />
          <p className="text-gray-600">Loading...</p>
        </div>
      </div>
    )
  }

  // Show setup message only when actually setting up a new free tier
  if (isSettingUp && !subscription) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <Loader2 className="w-8 h-8 animate-spin text-orange-500 mx-auto mb-4" />
          <p className="text-gray-600">Setting up your free MindRoom instance...</p>
        </div>
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
