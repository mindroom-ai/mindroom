'use client'

import { useAuth } from '@/hooks/useAuth'
import { useInstance } from '@/hooks/useInstance'
import { useSubscription } from '@/hooks/useSubscription'
import { InstanceCard } from '@/components/dashboard/InstanceCard'
import { UsageChart } from '@/components/dashboard/UsageChart'
import { QuickActions } from '@/components/dashboard/QuickActions'
import { Card, CardHeader } from '@/components/ui/Card'
import { Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { setSsoCookie, setupAccount } from '@/lib/api'

export default function DashboardPage() {
  const { user, loading: authLoading } = useAuth()
  const { instance, loading: instanceLoading } = useInstance()
  const { subscription, loading: subscriptionLoading } = useSubscription()
  const [isSettingUp, setIsSettingUp] = useState(false)
  const [setupAttempted, setSetupAttempted] = useState(false)
  const router = useRouter()

  useEffect(() => {
    // Ensure SSO cookie for instance access (no-op if not logged in)
    setSsoCookie().catch((e) => console.warn('Failed to set SSO cookie', e))
    // Refresh cookie periodically for longer sessions
    const id = setInterval(() => { setSsoCookie().catch((e) => console.warn('Failed to refresh SSO cookie', e)) }, 15 * 60 * 1000)

    // Auto-setup free tier if user has no subscription
    const setupFreeTier = async () => {
      // Skip if: not logged in, still loading, already has subscription, already setting up,
      // an instance already exists, or we've already attempted setup once in this session.
      if (
        authLoading ||
        !user ||
        subscriptionLoading ||
        instanceLoading ||
        subscription ||
        isSettingUp ||
        instance ||
        setupAttempted
      ) {
        return
      }

      setSetupAttempted(true)
      setIsSettingUp(true)
      try {
        await setupAccount()
        // Trigger a refresh; hooks poll and will pick up the new subscription
        router.refresh()
      } catch (error) {
        console.error('Error setting up free tier:', error)
      } finally {
        setIsSettingUp(false)
      }
    }

    setupFreeTier()
    return () => clearInterval(id)
  }, [authLoading, user, subscriptionLoading, subscription, isSettingUp, instance, instanceLoading, setupAttempted])

  if (authLoading || instanceLoading || subscriptionLoading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <Loader2 className="w-8 h-8 animate-spin text-orange-500 mx-auto mb-4" />
          <p className="text-gray-600 dark:text-gray-400">Loading...</p>
        </div>
      </div>
    )
  }

  // Show setup message only when actively setting up AND no instance exists yet
  if (isSettingUp && !subscription && !instance) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <Loader2 className="w-8 h-8 animate-spin text-orange-500 mx-auto mb-4" />
          <p className="text-gray-600 dark:text-gray-400">Setting up your free MindRoom instance...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-7xl mx-auto">
      {/* Welcome Header */}
      <Card>
        <h1 className="text-3xl font-bold bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">Welcome back!</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-2 text-lg">
          Your MindRoom is {instance?.status === 'running' ? '✅ up and running' : instance?.status === 'provisioning' ? '🔄 starting up' : '⏸️ currently offline'}
        </p>
      </Card>

      {/* Instance Status and Quick Actions */}
      <div className="grid lg:grid-cols-2 gap-6">
        <InstanceCard instance={instance} />
        <QuickActions instance={instance} subscription={subscription} />
      </div>

      {/* Usage Overview */}
      <Card>
        <CardHeader className="mb-6">Usage This Month</CardHeader>
        <UsageChart subscription={subscription} />
      </Card>
    </div>
  )
}
