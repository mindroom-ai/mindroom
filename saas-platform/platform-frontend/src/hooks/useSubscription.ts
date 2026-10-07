'use client'

import { useEffect, useState } from 'react'
import { useAuth } from './useAuth'
import { apiCall, type Subscription } from '@/lib/api'
import { subscriptionCache } from '@/lib/cache'
import { logger } from '@/lib/logger'

export type { Subscription }

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
