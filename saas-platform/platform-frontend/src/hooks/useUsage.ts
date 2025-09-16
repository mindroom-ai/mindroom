'use client'

import { useEffect, useState } from 'react'
import { useAuth } from './useAuth'
import { apiCall } from '@/lib/api'
import { cache } from '@/lib/cache'
import { logger } from '@/lib/logger'

export interface UsageMetrics {
  id: string
  subscription_id: string
  date: string
  messages_sent: number
  agents_used: number
  storage_used_gb: number
  created_at: string
}

export interface AggregatedUsage {
  totalMessages: number
  totalAgents: number
  totalStorage: number
  dailyUsage: UsageMetrics[]
}

export function useUsage(days: number = 30) {
  const cacheKey = `user-usage-${days}`
  const cachedUsage = cache.get(cacheKey) as AggregatedUsage | null
  const [usage, setUsage] = useState<AggregatedUsage | null>(cachedUsage)
  const [loading, setLoading] = useState(!cachedUsage)
  const { user } = useAuth()

  useEffect(() => {
    if (!user) {
      setLoading(false)
      return
    }

    // Get usage metrics through API
    const fetchUsage = async (isInitial = false) => {
      // Only show loading on initial fetch when there's no cached data
      if (isInitial && !cachedUsage) {
        setLoading(true)
      }

      try {
        const response = await apiCall(`/my/usage?days=${days}`)

        if (response.ok) {
          const data = await response.json()
          const usageData = {
            totalMessages: data.aggregated.totalMessages,
            totalAgents: data.aggregated.totalAgents,
            totalStorage: data.aggregated.totalStorage,
            dailyUsage: data.usage,
          }
          setUsage(usageData)
          cache.set(cacheKey, usageData)
        } else {
          logger.error('Error fetching usage:', response.statusText)
        }
      } catch (error) {
        logger.error('Error fetching usage:', error)
      } finally {
        if (isInitial) {
          setLoading(false)
        }
      }
    }

    fetchUsage(true)  // Initial fetch
  }, [user, days])

  return { usage, loading }
}
