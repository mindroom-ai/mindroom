'use client'

import { useState } from 'react'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'
import { Button } from '@/components/ui/button'
import { Play, Square, RotateCw, Trash2, Rocket } from 'lucide-react'

interface InstanceActionsProps {
  instanceId: string
  currentStatus: string
}

export function InstanceActions({ instanceId, currentStatus }: InstanceActionsProps) {
  const [loading, setLoading] = useState<string | null>(null)

  const performAction = async (action: 'start' | 'stop' | 'restart' | 'uninstall' | 'provision') => {
    setLoading(action)

    try {
      const method = action === 'uninstall' ? 'DELETE' : 'POST'
      const endpoint = action === 'uninstall'
        ? `/admin/instances/${instanceId}/uninstall`
        : action === 'provision'
        ? `/admin/instances/${instanceId}/provision`
        : `/admin/instances/${instanceId}/${action}`

      const response = await apiCall(endpoint, { method })

      if (!response.ok) {
        throw new Error(`Failed to ${action} instance`)
      }

      // Simple reload to refresh the status
      window.location.reload()
    } catch (error) {
      logger.error(`Failed to ${action} instance:`, error)
    } finally {
      setLoading(null)
    }
  }

  return (
    <div className="flex gap-2">
      {(currentStatus === 'deprovisioned' || currentStatus === 'error') && (
        <Button
          variant="ghost"
          size="sm"
          className="text-green-600 dark:text-green-400 hover:text-green-700 dark:hover:text-green-300"
          onClick={() => performAction('provision')}
          disabled={loading !== null}
        >
          <Rocket className="w-3 h-3 mr-1" />
          {loading === 'provision' ? 'Provisioning...' : 'Provision'}
        </Button>
      )}

      {currentStatus === 'stopped' && (
        <Button
          variant="ghost"
          size="sm"
          className="text-green-600 dark:text-green-400 hover:text-green-700 dark:hover:text-green-300"
          onClick={() => performAction('start')}
          disabled={loading !== null}
        >
          <Play className="w-3 h-3 mr-1" />
          {loading === 'start' ? 'Starting...' : 'Start'}
        </Button>
      )}

      {currentStatus === 'running' && (
        <Button
          variant="ghost"
          size="sm"
          className="text-yellow-600 dark:text-yellow-400 hover:text-yellow-700 dark:hover:text-yellow-300"
          onClick={() => performAction('stop')}
          disabled={loading !== null}
        >
          <Square className="w-3 h-3 mr-1" />
          {loading === 'stop' ? 'Stopping...' : 'Stop'}
        </Button>
      )}

      {currentStatus === 'running' && (
        <Button
          variant="ghost"
          size="sm"
          className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300"
          onClick={() => performAction('restart')}
          disabled={loading !== null}
        >
          <RotateCw className="w-3 h-3 mr-1" />
          {loading === 'restart' ? 'Restarting...' : 'Restart'}
        </Button>
      )}

      {currentStatus !== 'deprovisioned' && (
        <Button
          variant="ghost"
          size="sm"
          className="text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300"
          onClick={() => {
            if (confirm(`Uninstall instance ${instanceId}?`)) {
              performAction('uninstall')
            }
          }}
          disabled={loading !== null}
        >
          <Trash2 className="w-3 h-3 mr-1" />
          {loading === 'uninstall' ? 'Uninstalling...' : 'Uninstall'}
        </Button>
      )}
    </div>
  )
}
