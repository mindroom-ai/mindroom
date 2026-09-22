'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useAuth } from './useAuth'
import { restartInstance as apiRestartInstance, type Instance } from '@/lib/api'
import { cacheInstance, getCachedInstance, loadInstance } from '@/lib/instance-resource'
import { logger } from '@/lib/logger'

export type { Instance }

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
        tier: 'byok',
        created_at: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString(), // 7 days ago
        updated_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(), // 1 hour ago
      }
    : null

export function useInstance() {
  const { user, loading: authLoading } = useAuth()
  const userId = user?.id ?? null
  const cachedInstance = getCachedInstance(userId)
  const [snapshot, setSnapshot] = useState({ userId, instance: cachedInstance })
  const instance = snapshot.userId === userId ? snapshot.instance : cachedInstance
  const [loading, setLoading] = useState(!cachedInstance)
  const supabase = createClient()

  useEffect(() => {
    if (authLoading) return
    if (!user) {
      setLoading(false)
      return
    }

    setSnapshot({ userId, instance: getCachedInstance(userId) })

    // Use dev instance if in development mode
    if (DEV_INSTANCE) {
      setSnapshot({ userId, instance: DEV_INSTANCE })
      cacheInstance(user.id, DEV_INSTANCE)
      setLoading(false)
      return
    }

    let active = true
    // Get user's instance through the API endpoint
    const fetchInstance = async (isInitial = false) => {
      // Check for cached data right before deciding to show loading
      const currentCache = getCachedInstance(userId)

      // Only show loading on initial fetch when there's no cached data
      if (isInitial && !currentCache && !instance) {
        setLoading(true)
      }

      try {
        const loadedInstance = await loadInstance(user.id)
        if (active) setSnapshot({ userId, instance: loadedInstance })
      } catch (error) {
        logger.error('Error fetching instance:', error)
        // Show more details about the error
        if (error instanceof Error) {
          logger.error('Error details:', error.message)
        }
      } finally {
        if (isInitial && active) {
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
      active = false
      clearInterval(interval)
    }
  }, [user, authLoading, supabase])

  const restartInstance = async () => {
    if (!instance) return

    try {
      await apiRestartInstance(String(instance.instance_id))
      // Update local state to show restarting
      setSnapshot(prev => prev.userId === userId && prev.instance
        ? { ...prev, instance: { ...prev.instance, status: 'restarting' } }
        : prev)
    } catch (error) {
      logger.error('Error restarting instance:', error)
    }
  }

  return {
    instance,
    loading: authLoading || (userId !== null && snapshot.userId !== userId && !cachedInstance) || loading,
    restartInstance,
  }
}
