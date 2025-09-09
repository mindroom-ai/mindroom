'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useAuth } from './useAuth'
import { listInstances, restartInstance as apiRestartInstance } from '@/lib/api'

export interface Instance {
  id: string // UUID
  instance_id: number | string
  subscription_id: string
  subdomain: string
  status: 'provisioning' | 'running' | 'failed' | 'stopped' | 'error'
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
        const data = await listInstances()
        if (data.instances && data.instances.length > 0) {
          setInstance(data.instances[0])
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
    const interval = setInterval(async () => {
      try {
        const data = await listInstances()
        if (data.instances && data.instances.length > 0) {
          setInstance(data.instances[0])
        }
      } catch (err) {
        console.error('Error polling instance status:', err)
      }
    }, 10000)

    return () => {
      clearInterval(interval)
    }
  }, [user, supabase])

  const restartInstance = async () => {
    if (!instance) return

    try {
      await apiRestartInstance(String(instance.instance_id))
      // Update local state to show provisioning
      setInstance(prev => prev ? { ...prev, status: 'provisioning' } : null)
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
