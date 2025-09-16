'use client'

import { useAuth } from '@/hooks/useAuth'
import { useInstance } from '@/hooks/useInstance'
import { useSubscription } from '@/hooks/useSubscription'
import { InstanceCard } from '@/components/dashboard/InstanceCard'
import { UsageChart } from '@/components/dashboard/UsageChart'
import { QuickActions } from '@/components/dashboard/QuickActions'
import { DashboardLoader } from '@/components/dashboard/DashboardLoader'
import { Card, CardHeader } from '@/components/ui/Card'
import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { setSsoCookie, setupAccount } from '@/lib/api'
import { logger } from '@/lib/logger'

export default function DashboardPage() {
  const { user, loading: authLoading } = useAuth()
  const { instance, loading: instanceLoading } = useInstance()
  const { subscription, loading: subscriptionLoading } = useSubscription()
  const [isSettingUp, setIsSettingUp] = useState(false)
  const [setupAttempted, setSetupAttempted] = useState(false)
  const router = useRouter()

  useEffect(() => {
    // Ensure SSO cookie for instance access (no-op if not logged in)
    setSsoCookie().catch((e) => logger.warn('Failed to set SSO cookie', e))
    // Refresh cookie periodically for longer sessions
    const id = setInterval(() => { setSsoCookie().catch((e) => logger.warn('Failed to refresh SSO cookie', e)) }, 15 * 60 * 1000)

    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    // Auto-setup free tier if user has no subscription
    const setupFreeTier = async () => {
      // Skip if: not logged in, already has subscription, already setting up,
      // or we've already attempted setup once in this session.
      if (
        !user ||
        subscription ||
        isSettingUp ||
        setupAttempted
      ) {
        return
      }

      // Wait a bit for data to load before setting up
      if (authLoading || subscriptionLoading) {
        return
      }

      logger.log('Setting up free tier account...')
      setSetupAttempted(true)
      setIsSettingUp(true)
      try {
        const result = await setupAccount()
        logger.log('Free tier setup result:', result)
        // Trigger a refresh; hooks poll and will pick up the new subscription
        router.refresh()
        // Force reload after a short delay to ensure data is updated
        setTimeout(() => window.location.reload(), 2000)
      } catch (error) {
        logger.error('Error setting up free tier:', error)
      } finally {
        setIsSettingUp(false)
      }
    }

    setupFreeTier()
  }, [authLoading, user, subscriptionLoading, subscription, isSettingUp, setupAttempted, router])

  // Only show loading if we're still loading auth AND have no cached data
  // This prevents the flash of loading screen when navigating between pages
  if (authLoading && !instance && !subscription) {
    return <DashboardLoader />
  }

  // Also show loading if auth is done but we have no user and are still loading data
  if (!authLoading && !user && (instanceLoading || subscriptionLoading)) {
    return <DashboardLoader />
  }

  // Show setup message only when actively setting up AND no instance exists yet
  if (isSettingUp && !subscription && !instance) {
    return <DashboardLoader message="Setting up your free MindRoom instance..." />
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
