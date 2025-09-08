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

    // Get user's instance through the API endpoint (avoids RLS issues)
    const fetchInstance = async () => {
      try {
        const response = await fetch('/api/instance/status')
        if (response.ok) {
          const data = await response.json()
          if (data.instance) {
            setInstance(data.instance)
          }
        } else {
          console.error('Failed to fetch instance status')
        }
      } catch (error) {
        console.error('Error fetching instance:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchInstance()

    // Poll for changes every 10 seconds instead of realtime subscription
    // (avoids RLS issues with direct Supabase access)
    const interval = setInterval(() => {
      fetch('/api/instance/status')
        .then(res => res.json())
        .then(data => {
          if (data.instance) {
            setInstance(data.instance)
          }
        })
        .catch(err => console.error('Error polling instance status:', err))
    }, 10000)

    return () => {
      clearInterval(interval)
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
