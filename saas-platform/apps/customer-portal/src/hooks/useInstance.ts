'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useAuth } from './useAuth'

export interface Instance {
  id: string
  subscription_id: string
  subdomain: string
  status: 'provisioning' | 'running' | 'failed' | 'stopped'
  frontend_url: string | null
  backend_url: string | null
  created_at: string
  updated_at: string
}

export function useInstance() {
  const [instance, setInstance] = useState<Instance | null>(null)
  const [loading, setLoading] = useState(true)
  const { user } = useAuth()
  const supabase = createClient()

  useEffect(() => {
    if (!user) {
      setLoading(false)
      return
    }

    // Get user's instance through their account and subscription
    const fetchInstance = async () => {
      try {
        // First get the account by email
        const { data: account } = await supabase
          .from('accounts')
          .select('id')
          .eq('email', user.email)
          .single()

        if (account) {
          // Then get the user's subscription
          const { data: subscription } = await supabase
            .from('subscriptions')
            .select('id')
            .eq('account_id', account.id)
            .single()

          if (subscription) {
            // Finally get the instance for that subscription
            const { data: instanceData } = await supabase
              .from('instances')
              .select('*')
              .eq('subscription_id', subscription.id)
              .single()

            if (instanceData) {
              setInstance(instanceData)
            }
          }
        }
      } catch (error) {
        console.error('Error fetching instance:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchInstance()

    // Subscribe to changes (need to get subscription ID for proper filtering)
    const setupRealtimeSubscription = async () => {
      const { data: account } = await supabase
        .from('accounts')
        .select('id')
        .eq('email', user.email)
        .single()

      if (account) {
        const { data: subscriptionData } = await supabase
          .from('subscriptions')
          .select('id')
          .eq('account_id', account.id)
          .single()

        if (subscriptionData) {
          const subscription = supabase
            .channel('instance-changes')
            .on(
              'postgres_changes',
              {
                event: '*',
                schema: 'public',
                table: 'instances',
                filter: `subscription_id=eq.${subscriptionData.id}`,
              },
              (payload) => {
                setInstance(payload.new as Instance)
              }
            )
            .subscribe()

          return () => {
            subscription.unsubscribe()
          }
        }
      }
    }

    const cleanup = setupRealtimeSubscription()
    return () => {
      cleanup.then(fn => fn && fn())
    }
  }, [user, supabase])

  const restartInstance = async () => {
    if (!instance) return

    try {
      const response = await fetch('/api/instance/restart', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ instanceId: instance.id }),
      })

      if (response.ok) {
        // Update local state to show provisioning
        setInstance(prev => prev ? { ...prev, status: 'provisioning' } : null)
      }
    } catch (error) {
      console.error('Error restarting instance:', error)
    }
  }

  return {
    instance,
    loading,
    restartInstance,
  }
}
