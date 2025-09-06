import { createClient } from '@supabase/supabase-js'
import { config } from '../config'

const supabase = createClient(config.supabaseUrl, config.supabaseServiceKey)

export interface InstanceHealth {
  id: string
  dokku_app_name: string
  status: 'running' | 'stopped' | 'failed' | 'provisioning'
  health_status: 'healthy' | 'unhealthy' | 'unknown'
  cpu_usage?: number
  memory_usage?: number
  disk_usage?: number
  last_check: Date
}

export interface PlatformMetrics {
  totalInstances: number
  runningInstances: number
  failedInstances: number
  totalMessages: number
  activeUsers: number
  apiCallsToday: number
}

export const monitoringService = {
  async getInstanceHealth(instanceId: string): Promise<InstanceHealth | null> {
    const { data, error } = await supabase
      .from('instances')
      .select('*')
      .eq('id', instanceId)
      .single()

    if (error || !data) return null

    // In a real implementation, you'd also call the Dokku API to get real-time metrics
    return {
      id: data.id,
      dokku_app_name: data.dokku_app_name,
      status: data.status,
      health_status: data.health_status || 'unknown',
      last_check: new Date(data.last_health_check || Date.now()),
    }
  },

  async getAllInstancesHealth(): Promise<InstanceHealth[]> {
    const { data, error } = await supabase
      .from('instances')
      .select('*')
      .order('created_at', { ascending: false })

    if (error || !data) return []

    return data.map(instance => ({
      id: instance.id,
      dokku_app_name: instance.dokku_app_name,
      status: instance.status,
      health_status: instance.health_status || 'unknown',
      last_check: new Date(instance.last_health_check || Date.now()),
    }))
  },

  async getPlatformMetrics(): Promise<PlatformMetrics> {
    // Get instance counts
    const { data: instances } = await supabase
      .from('instances')
      .select('status')

    const instanceCounts = instances?.reduce((acc, inst) => {
      acc.total++
      if (inst.status === 'running') acc.running++
      if (inst.status === 'failed') acc.failed++
      return acc
    }, { total: 0, running: 0, failed: 0 }) || { total: 0, running: 0, failed: 0 }

    // Get active users (logged in within last 24 hours)
    const yesterday = new Date()
    yesterday.setDate(yesterday.getDate() - 1)

    const { count: activeUsers } = await supabase
      .from('accounts')
      .select('*', { count: 'exact', head: true })
      .gte('last_login', yesterday.toISOString())

    // Get today's metrics
    const today = new Date().toISOString().split('T')[0]
    const { data: todayMetrics } = await supabase
      .from('usage_metrics')
      .select('messages_sent, api_calls')
      .eq('metric_date', today)

    const totals = todayMetrics?.reduce((acc, metric) => {
      acc.messages += metric.messages_sent || 0
      acc.apiCalls += metric.api_calls || 0
      return acc
    }, { messages: 0, apiCalls: 0 }) || { messages: 0, apiCalls: 0 }

    return {
      totalInstances: instanceCounts.total,
      runningInstances: instanceCounts.running,
      failedInstances: instanceCounts.failed,
      totalMessages: totals.messages,
      activeUsers: activeUsers || 0,
      apiCallsToday: totals.apiCalls,
    }
  },

  async checkAndUpdateHealth(instanceId: string): Promise<void> {
    // In a real implementation, this would call the Dokku API
    // to get actual health metrics and update the database

    const health = await this.getInstanceHealth(instanceId)
    if (!health) return

    // Update the last health check timestamp
    await supabase
      .from('instances')
      .update({
        last_health_check: new Date().toISOString(),
        health_status: health.health_status,
      })
      .eq('id', instanceId)
  },

  async getAlerts(severity?: 'critical' | 'warning' | 'info'): Promise<any[]> {
    // In a real implementation, this would fetch alerts from a monitoring system
    // For now, we'll check for basic issues in the database

    const alerts = []

    // Check for failed instances
    const { data: failedInstances } = await supabase
      .from('instances')
      .select('*')
      .eq('status', 'failed')

    if (failedInstances && failedInstances.length > 0) {
      alerts.push({
        severity: 'critical',
        message: `${failedInstances.length} instances are in failed state`,
        timestamp: new Date(),
        instances: failedInstances.map(i => i.dokku_app_name),
      })
    }

    // Check for overdue payments
    const { data: overdueSubscriptions } = await supabase
      .from('subscriptions')
      .select('*')
      .eq('status', 'past_due')

    if (overdueSubscriptions && overdueSubscriptions.length > 0) {
      alerts.push({
        severity: 'warning',
        message: `${overdueSubscriptions.length} subscriptions have overdue payments`,
        timestamp: new Date(),
        count: overdueSubscriptions.length,
      })
    }

    return severity
      ? alerts.filter(alert => alert.severity === severity)
      : alerts
  },
}
