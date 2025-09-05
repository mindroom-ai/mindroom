'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useAuth } from './useAuth'

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
  const supabase = createClient()

  useEffect(() => {
    if (!user) {
      setLoading(false)
      return
    }

    // Get user's subscription
    const fetchSubscription = async () => {
      try {
        const { data } = await supabase
          .from('subscriptions')
          .select('*')
          .eq('account_id', user.id)
          .single()

        if (data) {
          setSubscription(data)
        }
      } catch (error) {
        console.error('Error fetching subscription:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchSubscription()

    // Subscribe to changes
    const subscription = supabase
      .channel('subscription-changes')
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'subscriptions',
          filter: `account_id=eq.${user.id}`,
        },
        (payload) => {
          setSubscription(payload.new as Subscription)
        }
      )
      .subscribe()

    return () => {
      subscription.unsubscribe()
    }
  }, [user, supabase])

  return { subscription, loading }
}
