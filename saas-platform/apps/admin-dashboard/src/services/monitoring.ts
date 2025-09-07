import { config } from '../config'

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
    try {
      const response = await fetch(`${config.apiUrl}/instances/${instanceId}`)
      if (!response.ok) return null

      const result = await response.json()
      const data = result.data

      return {
        id: data.id,
        dokku_app_name: data.dokku_app_name,
        status: data.status,
        health_status: data.health_status || 'unknown',
        last_check: new Date(data.last_health_check || Date.now()),
      }
    } catch (error) {
      console.error('Error fetching instance health:', error)
      return null
    }
  },

  async getAllInstancesHealth(): Promise<InstanceHealth[]> {
    try {
      const response = await fetch(`${config.apiUrl}/instances?_sort=created_at&_order=DESC`)
      if (!response.ok) return []

      const result = await response.json()

      return result.data.map((instance: any) => ({
        id: instance.id,
        dokku_app_name: instance.dokku_app_name,
        status: instance.status,
        health_status: instance.health_status || 'unknown',
        last_check: new Date(instance.last_health_check || Date.now()),
      }))
    } catch (error) {
      console.error('Error fetching all instances health:', error)
      return []
    }
  },

  async getPlatformMetrics(): Promise<PlatformMetrics> {
    try {
      const response = await fetch(`${config.apiUrl}/metrics/platform`)
      if (!response.ok) {
        return {
          totalInstances: 0,
          runningInstances: 0,
          failedInstances: 0,
          totalMessages: 0,
          activeUsers: 0,
          apiCallsToday: 0,
        }
      }

      const data = await response.json()
      return data
    } catch (error) {
      console.error('Error fetching platform metrics:', error)
      return {
        totalInstances: 0,
        runningInstances: 0,
        failedInstances: 0,
        totalMessages: 0,
        activeUsers: 0,
        apiCallsToday: 0,
      }
    }
  },

  async checkAndUpdateHealth(instanceId: string): Promise<void> {
    try {
      const response = await fetch(`${config.apiUrl}/instances/${instanceId}/health`, {
        method: 'POST',
      })

      if (!response.ok) {
        console.error('Failed to update health check')
      }
    } catch (error) {
      console.error('Error updating health check:', error)
    }
  },

  async getAlerts(severity?: 'critical' | 'warning' | 'info'): Promise<any[]> {
    try {
      const url = severity
        ? `${config.apiUrl}/alerts?severity=${severity}`
        : `${config.apiUrl}/alerts`

      const response = await fetch(url)
      if (!response.ok) return []

      const data = await response.json()
      return data
    } catch (error) {
      console.error('Error fetching alerts:', error)
      return []
    }
  },
}
