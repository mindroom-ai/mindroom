'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface UsageMetric {
  id: string
  account_id: string
  created_at: string
  message_count: number
  storage_mb: number
  api_calls: number
  agent_count: number
  accounts?: {
    email: string
    full_name: string | null
  }
}

export default function UsagePage() {
  const [metrics, setMetrics] = useState<UsageMetric[]>([])
  const [loading, setLoading] = useState(true)

  // Calculate summary statistics
  const totalMessages = metrics?.reduce((sum, m) => sum + (m.message_count || 0), 0) || 0
  const totalStorage = metrics?.reduce((sum, m) => sum + (m.storage_mb || 0), 0) || 0
  const uniqueUsers = new Set(metrics?.map(m => m.account_id)).size

  useEffect(() => {
    const fetchUsageMetrics = async () => {
      try {
        // Get usage metrics for the last 30 days
        const thirtyDaysAgo = new Date()
        thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30)

        // Use the generic admin endpoint with date filter
        const response = await apiCall(
          `/admin/usage_metrics?_sort=created_at&_order=DESC&created_at_gte=${thirtyDaysAgo.toISOString()}`
        )

        if (response.ok) {
          const data = await response.json()
          setMetrics(data.data || [])
        } else {
          logger.error('Failed to fetch usage metrics:', response.statusText)
        }
      } catch (error) {
        logger.error('Error fetching usage metrics:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchUsageMetrics()
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-lg">Loading...</div>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Usage Metrics</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-2">Platform usage statistics and trends</p>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">Total Messages (30d)</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{totalMessages.toLocaleString()}</div>
          </CardContent>
        </Card>

        <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">Storage Used</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{(totalStorage / 1024).toFixed(2)} GB</div>
          </CardContent>
        </Card>

        <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-gray-700 dark:text-gray-300">Active Users (30d)</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{uniqueUsers}</div>
          </CardContent>
        </Card>
      </div>

      {/* Detailed Usage Table */}
      <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
        <CardHeader>
          <CardTitle className="text-gray-900 dark:text-gray-100">Detailed Usage</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Date</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Customer</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Messages</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Storage (MB)</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">API Calls</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Agents</th>
                </tr>
              </thead>
              <tbody>
                {metrics?.map((metric) => (
                  <tr key={metric.id} className="border-b border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">
                    <td className="py-3 px-4 text-sm text-gray-700 dark:text-gray-300">
                      {new Date(metric.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4">
                      <div>
                        <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                          {metric.accounts?.email || 'Unknown'}
                        </div>
                        <div className="text-xs text-gray-500 dark:text-gray-400">
                          {metric.accounts?.full_name}
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-700 dark:text-gray-300">
                      {metric.message_count || 0}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-700 dark:text-gray-300">
                      {metric.storage_mb || 0}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-700 dark:text-gray-300">
                      {metric.api_calls || 0}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-700 dark:text-gray-300">
                      {metric.agent_count || 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {(!metrics || metrics.length === 0) && (
              <div className="text-center py-8 text-gray-500">
                No usage data available
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
