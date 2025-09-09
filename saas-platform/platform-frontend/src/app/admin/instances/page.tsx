'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { InstanceActions } from '@/components/admin/InstanceActions'
import { apiCall } from '@/lib/api'

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

  useEffect(() => {
    const fetchInstances = async () => {
      try {
        const response = await apiCall('/api/admin/instances')
        if (response.ok) {
          const data = await response.json()
          // Generic admin list endpoint returns { data, total }
          setInstances(data.data || [])
        } else {
          console.error('Failed to fetch instances:', response.statusText)
        }
      } catch (error) {
        console.error('Error fetching instances:', error)
      } finally {
        setLoading(false)
      }
    }

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
          <h1 className="text-3xl font-bold text-gray-900">Instances</h1>
          <p className="text-gray-600 mt-2">Manage customer MindRoom instances</p>
        </div>
        <div className="space-x-2">
          <Button variant="outline">Refresh All</Button>
          <Button>Deploy New</Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>All Instances</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-3 px-4">Instance ID</th>
                  <th className="text-left py-3 px-4">Customer</th>
                  <th className="text-left py-3 px-4">Status</th>
                  <th className="text-left py-3 px-4">URL</th>
                  <th className="text-left py-3 px-4">Agents</th>
                  <th className="text-left py-3 px-4">Created</th>
                  <th className="text-left py-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {instances?.map((instance) => (
                  <tr key={instance.id} className="border-b hover:bg-gray-50">
                    <td className="py-3 px-4">
                      <div className="font-mono text-sm">
                        {instance.instance_id}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <div>
                        <div className="font-medium">
                          {instance.accounts?.email}
                        </div>
                        <div className="text-sm text-gray-500">
                          {instance.accounts?.full_name}
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        instance.status === 'running' ? 'bg-green-100 text-green-800' :
                        instance.status === 'stopped' ? 'bg-gray-100 text-gray-800' :
                        instance.status === 'error' ? 'bg-red-100 text-red-800' :
                        instance.status === 'provisioning' ? 'bg-blue-100 text-blue-800' :
                        instance.status === 'deprovisioned' ? 'bg-gray-100 text-gray-800' :
                        'bg-yellow-100 text-yellow-800'
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
                          className="text-blue-600 hover:text-blue-900 text-sm"
                        >
                          {instance.instance_url.replace('https://', '')}
                        </a>
                      ) : (
                        <span className="text-gray-400">-</span>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm">
                        {instance.agent_count || 0}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500">
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
