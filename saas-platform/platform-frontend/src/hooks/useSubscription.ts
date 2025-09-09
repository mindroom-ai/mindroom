'use client'

import { useEffect, useState } from 'react'
import { useAuth } from './useAuth'
import { apiCall } from '@/lib/api'

export interface Subscription {
  id: string
  account_id: string
  tier: 'free' | 'starter' | 'professional' | 'enterprise'
  status: 'active' | 'cancelled' | 'past_due'
  stripe_subscription_id: string | null
  stripe_customer_id: string | null
  current_period_end: string | null
  max_agents: number
  max_messages_per_day: number
  max_storage_gb: number
  created_at: string
  updated_at: string
}

export function useSubscription() {
  const [subscription, setSubscription] = useState<Subscription | null>(null)
  const [loading, setLoading] = useState(true)
  const { user } = useAuth()

  useEffect(() => {
    if (!user) {
      setLoading(false)
      return
    }

    // Get user's subscription through API
    const fetchSubscription = async () => {
      try {
        const response = await apiCall('/api/v1/subscription')

        if (response.ok) {
          const data = await response.json()
          setSubscription(data)
        } else if (response.status === 404) {
          // User doesn't have a subscription yet
          setSubscription(null)
        } else {
          console.error('Error fetching subscription:', response.statusText)
        }
      } catch (error) {
        console.error('Error fetching subscription:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchSubscription()

    // Poll for updates every 30 seconds instead of using real-time
    const interval = setInterval(fetchSubscription, 30000)

    return () => {
      clearInterval(interval)
    }
  }, [user])

  return { subscription, loading }
}
