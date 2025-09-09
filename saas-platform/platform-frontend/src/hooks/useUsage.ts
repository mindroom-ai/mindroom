'use client'

import { useEffect, useState } from 'react'
import { useAuth } from './useAuth'
import { apiCall } from '@/lib/api'

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
  const [usage, setUsage] = useState<AggregatedUsage | null>(null)
  const [loading, setLoading] = useState(true)
  const { user } = useAuth()

  useEffect(() => {
    if (!user) {
      setLoading(false)
      return
    }

    // Get usage metrics through API
    const fetchUsage = async () => {
      try {
        const response = await apiCall(`/api/v1/usage?days=${days}`)

        if (response.ok) {
          const data = await response.json()
          setUsage({
            totalMessages: data.aggregated.totalMessages,
            totalAgents: data.aggregated.totalAgents,
            totalStorage: data.aggregated.totalStorage,
            dailyUsage: data.usage,
          })
        } else {
          console.error('Error fetching usage:', response.statusText)
        }
      } catch (error) {
        console.error('Error fetching usage:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchUsage()
  }, [user, days])

  return { usage, loading }
}
