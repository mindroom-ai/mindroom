'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useAuth } from './useAuth'
import { listInstances, restartInstance as apiRestartInstance } from '@/lib/api'
import { cache } from '@/lib/cache'

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
  const cachedInstance = cache.get('user-instance') as Instance | null
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
      cache.set('user-instance', DEV_INSTANCE)
      setLoading(false)
      return
    }

    // Get user's instance through the API endpoint
    const fetchInstance = async () => {
      try {
        const data = await listInstances()
        if (data.instances && data.instances.length > 0) {
          const newInstance = data.instances[0]
          setInstance(newInstance)
          cache.set('user-instance', newInstance)
        } else {
          // No instances found
          setInstance(null)
        }
      } catch (error) {
        console.error('Error fetching instance:', error)
        // Show more details about the error
        if (error instanceof Error) {
          console.error('Error details:', error.message)
        }
      } finally {
        setLoading(false)
      }
    }

    fetchInstance()

    // Skip polling in dev mode
    if (DEV_INSTANCE) {
      return
    }

    // Poll for changes every 30 seconds instead of realtime subscription
    // (avoids RLS issues with direct Supabase access)
    let errorCount = 0
    const interval = setInterval(async () => {
      try {
        const data = await listInstances()
        if (data.instances && data.instances.length > 0) {
          const newInstance = data.instances[0]
          // Only update if status actually changed to avoid re-renders
          if (newInstance.status !== instance?.status) {
            setInstance(newInstance)
            cache.set('user-instance', newInstance)
          }
        }
        errorCount = 0 // Reset error count on success
      } catch (err) {
        errorCount++
        // Only log first error and every 10th error to avoid spamming
        if (errorCount === 1 || errorCount % 10 === 0) {
          console.error(`Error polling instance status (attempt ${errorCount}):`, err)
        }
      }
    }, 30000)

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
      console.error('Error restarting instance:', error)
    }
  }

  return {
    instance,
    loading,
    restartInstance,
  }
}
