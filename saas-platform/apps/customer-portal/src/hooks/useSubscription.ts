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

    // Get user's subscription through their account
    const fetchSubscription = async () => {
      try {
        // First get the account by email
        const { data: account } = await supabase
          .from('accounts')
          .select('id')
          .eq('email', user.email)
          .single()

        if (account) {
          // Then get the subscription for that account
          const { data } = await supabase
            .from('subscriptions')
            .select('*')
            .eq('account_id', account.id)
            .single()

          if (data) {
            setSubscription(data)
          }
        }
      } catch (error) {
        console.error('Error fetching subscription:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchSubscription()

    // Subscribe to changes (we need to get account ID first for proper filtering)
    const setupRealtimeSubscription = async () => {
      const { data: account } = await supabase
        .from('accounts')
        .select('id')
        .eq('email', user.email)
        .single()

      if (account) {
        const subscription = supabase
          .channel('subscription-changes')
          .on(
            'postgres_changes',
            {
              event: '*',
              schema: 'public',
              table: 'subscriptions',
              filter: `account_id=eq.${account.id}`,
            },
            (payload) => {
              setSubscription(payload.new as Subscription)
            }
          )
          .subscribe()

        return () => {
          subscription.unsubscribe()
        }
      }
    }

    const cleanup = setupRealtimeSubscription()
    return () => {
      cleanup.then(fn => fn && fn())
    }
  }, [user, supabase])

  return { subscription, loading }
}
