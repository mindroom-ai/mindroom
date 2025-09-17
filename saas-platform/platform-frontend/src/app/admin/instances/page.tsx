'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/button'
import { InstanceActions } from '@/components/admin/InstanceActions'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface Instance {
  id: string
  account_id: string
  instance_id: number | string
  subdomain: string
  status: string
  instance_url: string | null
  agent_count: number
  created_at: string
  accounts?: {
    email: string
    full_name: string | null
  }
}

export default function InstancesPage() {
  const [instances, setInstances] = useState<Instance[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)

  const fetchInstances = async () => {
    try {
      setLoading(true)
      const response = await apiCall('/admin/instances')
      if (response.ok) {
        const data = await response.json()
        // Generic admin list endpoint returns { data, total }
        setInstances(data.data || [])
      } else {
        logger.error('Failed to fetch instances:', response.statusText)
      }
    } catch (error) {
      logger.error('Error fetching instances:', error)
    } finally {
      setLoading(false)
    }
  }

  const syncInstances = async () => {
    setSyncing(true)
    try {
      const response = await apiCall('/admin/sync-instances', { method: 'POST' })
      if (response.ok) {
        await response.json()
        // Refresh the instances list after sync
        await fetchInstances()
      } else {
        logger.error('Failed to sync instances')
      }
    } catch (error) {
      logger.error('Failed to sync instances:', error)
    } finally {
      setSyncing(false)
    }
  }

  useEffect(() => {
    fetchInstances()
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
      <div className="mb-8 flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Instances</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-2">Manage customer MindRoom instances</p>
        </div>
        <div className="space-x-2">
          <Button
            variant="outline"
            onClick={syncInstances}
            disabled={syncing}
          >
            {syncing ? 'Syncing...' : 'Refresh All'}
          </Button>
          <Button>Deploy New</Button>
        </div>
      </div>

      <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
        <CardHeader>
          <CardTitle className="text-gray-900 dark:text-gray-100">All Instances</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Instance ID</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Customer</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Status</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">URL</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Agents</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Created</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Actions</th>
                </tr>
              </thead>
              <tbody>
                {instances?.map((instance) => (
                  <tr key={instance.id} className="border-b border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">
                    <td className="py-3 px-4">
                      <div className="font-mono text-sm text-gray-900 dark:text-gray-100">
                        {instance.instance_id}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <div>
                        <div className="font-medium text-gray-900 dark:text-gray-100">
                          {instance.accounts?.email || 'No email'}
                        </div>
                        <div className="text-sm text-gray-500 dark:text-gray-400">
                          {instance.accounts?.full_name || '-'}
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        instance.status === 'running' ? 'bg-green-100 dark:bg-green-900/20 text-green-800 dark:text-green-400' :
                        instance.status === 'stopped' ? 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-400' :
                        instance.status === 'error' ? 'bg-red-100 dark:bg-red-900/20 text-red-800 dark:text-red-400' :
                        instance.status === 'provisioning' ? 'bg-blue-100 dark:bg-blue-900/20 text-blue-800 dark:text-blue-400' :
                        instance.status === 'deprovisioned' ? 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-400' :
                        'bg-yellow-100 dark:bg-yellow-900/20 text-yellow-800 dark:text-yellow-400'
                      }`}>
                        {instance.status}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      {instance.instance_url ? (
                        <a
                          href={instance.instance_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 text-sm"
                        >
                          {instance.instance_url.replace('https://', '')}
                        </a>
                      ) : (
                        <span className="text-gray-400 dark:text-gray-500">-</span>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm text-gray-900 dark:text-gray-100">
                        {instance.agent_count || 0}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                      {new Date(instance.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4">
                      <InstanceActions
                        instanceId={instance.instance_id || instance.subdomain}
                        currentStatus={instance.status}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {(!instances || instances.length === 0) && (
              <div className="text-center py-8 text-gray-500">
                No instances found
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
