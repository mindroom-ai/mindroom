'use client'

import { useEffect, useState } from 'react'
import { useAuth } from './useAuth'
import { apiCall } from '@/lib/api'
import { subscriptionCache } from '@/lib/cache'
import { logger } from '@/lib/logger'

export interface Subscription {
  id: string
  account_id: string
  tier: 'free' | 'starter' | 'professional' | 'enterprise'
  status: 'active' | 'cancelled' | 'past_due' | 'trialing'
  stripe_subscription_id: string | null
  stripe_customer_id: string | null
  current_period_start: string | null
  current_period_end: string | null
  trial_ends_at: string | null
  cancelled_at: string | null
  max_agents: number
  max_messages_per_day: number
  max_storage_gb: number
  created_at: string
  updated_at: string
}

export function useSubscription() {
  const cachedSubscription = subscriptionCache.get('user-subscription') as Subscription | null
  const [subscription, setSubscription] = useState<Subscription | null>(cachedSubscription)
  const [loading, setLoading] = useState(!cachedSubscription)
  const { user, loading: authLoading } = useAuth()

  const fetchSubscription = async (isInitial = false, forceRefresh = false) => {
    if (!user) return

    // Clear cache if force refresh is requested
    if (forceRefresh) {
      subscriptionCache.delete('user-subscription')
    }

    // Check for cached data right before deciding to show loading
    const currentCache = subscriptionCache.get('user-subscription') as Subscription | null

    // Only show loading on initial fetch when there's no cached data
    if (isInitial && !currentCache && !subscription) {
      setLoading(true)
    }

    try {
      const response = await apiCall('/my/subscription')

      if (response.ok) {
        const data = await response.json()
        setSubscription(data)
        subscriptionCache.set('user-subscription', data)
      } else if (response.status === 404) {
        setSubscription(null)
        subscriptionCache.delete('user-subscription')
      } else {
        logger.error('Error fetching subscription:', response.statusText)
      }
    } catch (error) {
      logger.error('Error fetching subscription:', error)
    } finally {
      if (isInitial) {
        setLoading(false)
      }
    }
  }

  useEffect(() => {
    if (authLoading) return
    if (!user) {
      setLoading(false)
      return
    }

    fetchSubscription(true)  // Initial fetch

    // Poll for updates every 10 seconds for more responsive updates
    const interval = setInterval(() => fetchSubscription(false, false), 10000)  // Background updates

    return () => {
      clearInterval(interval)
    }
  }, [user, authLoading])

  return { subscription, loading, refresh: (forceRefresh = true) => fetchSubscription(false, forceRefresh) }
}
