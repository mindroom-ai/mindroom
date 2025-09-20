'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useAuth } from './useAuth'
import { listInstances, restartInstance as apiRestartInstance } from '@/lib/api'
import { instanceCache } from '@/lib/cache'
import { logger } from '@/lib/logger'

export interface Instance {
  id: string // UUID
  instance_id: number | string
  subscription_id: string
  subdomain: string
  status: 'provisioning' | 'running' | 'failed' | 'stopped' | 'error' | 'deprovisioned' | 'restarting'
  frontend_url: string | null
  backend_url: string | null
  created_at: string
  updated_at: string
  tier?: string
  matrix_server_url?: string | null
  kubernetes_synced_at?: string | null
  status_hint?: string | null
}

// Development-only mock instance
const DEV_INSTANCE: Instance | null =
  process.env.NODE_ENV === 'development' &&
  process.env.NEXT_PUBLIC_DEV_AUTH === 'true'
    ? {
        id: 'dev-instance-123',
        instance_id: 1,
        subscription_id: 'dev-sub-123',
        subdomain: 'dev',
        status: 'running',
        frontend_url: 'https://dev.mindroom.local',
        backend_url: 'https://api.dev.mindroom.local',
        matrix_server_url: 'https://matrix.dev.mindroom.local',
        tier: 'starter',
        created_at: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString(), // 7 days ago
        updated_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(), // 1 hour ago
      }
    : null

export function useInstance() {
  const cachedInstance = instanceCache.get('user-instance') as Instance | null
  const [instance, setInstance] = useState<Instance | null>(cachedInstance)
  const [loading, setLoading] = useState(!cachedInstance)
  const { user, loading: authLoading } = useAuth()
  const supabase = createClient()

  useEffect(() => {
    if (authLoading) return
    if (!user) {
      setLoading(false)
      return
    }

    // Use dev instance if in development mode
    if (DEV_INSTANCE) {
      setInstance(DEV_INSTANCE)
      instanceCache.set('user-instance', DEV_INSTANCE)
      setLoading(false)
      return
    }

    // Get user's instance through the API endpoint
    const fetchInstance = async (isInitial = false) => {
      // Check for cached data right before deciding to show loading
      const currentCache = instanceCache.get('user-instance') as Instance | null

      // Only show loading on initial fetch when there's no cached data
      if (isInitial && !currentCache && !instance) {
        setLoading(true)
      }

      try {
        const data = await listInstances()
        if (data.instances && data.instances.length > 0) {
          const newInstance = data.instances[0]
          setInstance(newInstance)
          instanceCache.set('user-instance', newInstance)
        } else {
          // No instances found
          setInstance(null)
          instanceCache.delete('user-instance')
        }
      } catch (error) {
        logger.error('Error fetching instance:', error)
        // Show more details about the error
        if (error instanceof Error) {
          logger.error('Error details:', error.message)
        }
      } finally {
        if (isInitial) {
          setLoading(false)
        }
      }
    }

    fetchInstance(true)  // Initial fetch

    // Skip polling in dev mode
    if (DEV_INSTANCE) {
      return
    }

    // Poll for changes every 15 seconds for more responsive updates
    // (avoids RLS issues with direct Supabase access)
    const interval = setInterval(async () => {
      await fetchInstance(false)  // Background update, no loading state
    }, 15000)

    return () => {
      clearInterval(interval)
    }
  }, [user, authLoading, supabase])

  const restartInstance = async () => {
    if (!instance) return

    try {
      await apiRestartInstance(String(instance.instance_id))
      // Update local state to show restarting
      setInstance(prev => prev ? { ...prev, status: 'restarting' } : null)
    } catch (error) {
      logger.error('Error restarting instance:', error)
    }
  }

  return {
    instance,
    loading,
    restartInstance,
  }
}
