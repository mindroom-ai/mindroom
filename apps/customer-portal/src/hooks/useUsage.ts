'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useSubscription } from './useSubscription'

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
  const { subscription } = useSubscription()
  const supabase = createClient()

  useEffect(() => {
    if (!subscription) {
      setLoading(false)
      return
    }

    // Get usage metrics for the last N days
    const fetchUsage = async () => {
      try {
        const startDate = new Date()
        startDate.setDate(startDate.getDate() - days)

        const { data } = await supabase
          .from('usage_metrics')
          .select('*')
          .eq('subscription_id', subscription.id)
          .gte('date', startDate.toISOString().split('T')[0])
          .order('date', { ascending: true })

        if (data) {
          // Aggregate the usage data
          const aggregated: AggregatedUsage = {
            totalMessages: data.reduce((sum, d) => sum + d.messages_sent, 0),
            totalAgents: Math.max(...data.map(d => d.agents_used), 0),
            totalStorage: Math.max(...data.map(d => d.storage_used_gb), 0),
            dailyUsage: data,
          }
          setUsage(aggregated)
        }
      } catch (error) {
        console.error('Error fetching usage:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchUsage()
  }, [subscription, days, supabase])

  return { usage, loading }
}
