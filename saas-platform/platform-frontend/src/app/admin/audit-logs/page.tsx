'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface AuditLog {
  id: string
  created_at: string
  account_id: string | null
  action: string
  resource_type: string
  resource_id: string | null
  details: any
  ip_address: string | null
  accounts?: {
    email: string
  }
}

export default function AuditLogsPage() {
  const [logs, setLogs] = useState<AuditLog[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchAuditLogs = async () => {
      try {
        // Get last 100 audit logs
        const response = await apiCall(
          '/admin/audit_logs?_sort=created_at&_order=DESC&_start=0&_end=100'
        )

        if (response.ok) {
          const data = await response.json()
          setLogs(data.data || [])
        } else {
          logger.error('Failed to fetch audit logs:', response.statusText)
        }
      } catch (error) {
        logger.error('Error fetching audit logs:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchAuditLogs()
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
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Audit Logs</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-2">System activity and security events</p>
      </div>

      <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
        <CardHeader>
          <CardTitle className="text-gray-900 dark:text-gray-100">Recent Activity</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Time</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">User</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Action</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Resource</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Details</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">IP Address</th>
                </tr>
              </thead>
              <tbody>
                {logs?.map((log) => (
                  <tr key={log.id} className="border-b border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">
                    <td className="py-3 px-4 text-sm text-gray-700 dark:text-gray-300">
                      {new Date(log.created_at).toLocaleString()}
                    </td>
                    <td className="py-3 px-4">
                      <div className="text-sm text-gray-900 dark:text-gray-100">
                        {log.accounts?.email || 'System'}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        log.action === 'create' ? 'bg-green-100 dark:bg-green-900/20 text-green-800 dark:text-green-400' :
                        log.action === 'update' ? 'bg-blue-100 dark:bg-blue-900/20 text-blue-800 dark:text-blue-400' :
                        log.action === 'delete' ? 'bg-red-100 dark:bg-red-900/20 text-red-800 dark:text-red-400' :
                        'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-400'
                      }`}>
                        {log.action}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-700 dark:text-gray-300">
                      {log.resource_type}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                      {log.details ? JSON.stringify(log.details).substring(0, 50) + '...' : '-'}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                      {log.ip_address || '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {(!logs || logs.length === 0) && (
              <div className="text-center py-8 text-gray-500">
                No audit logs found
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
