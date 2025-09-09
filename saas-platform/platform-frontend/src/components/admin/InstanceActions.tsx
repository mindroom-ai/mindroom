'use client'

import { useState } from 'react'

interface InstanceActionsProps {
  instanceId: string
  currentStatus: string
}

export function InstanceActions({ instanceId, currentStatus }: InstanceActionsProps) {
  const [loading, setLoading] = useState<string | null>(null)

  const performAction = async (action: 'start' | 'stop' | 'restart' | 'uninstall') => {
    setLoading(action)

    try {
      const apiKey = process.env.NEXT_PUBLIC_PROVISIONER_API_KEY || 'test-key'
      const baseUrl = process.env.NEXT_PUBLIC_PLATFORM_BACKEND_URL || '/api'

      const method = action === 'uninstall' ? 'DELETE' : 'POST'
      const endpoint = `/v1/${action}/${instanceId}`

      const response = await fetch(`${baseUrl}${endpoint}`, {
        method,
        headers: {
          'Authorization': `Bearer ${apiKey}`,
        },
      })

      if (!response.ok) {
        throw new Error(`Failed to ${action} instance`)
      }

      // Simple reload to refresh the status
      window.location.reload()
    } catch (error) {
      console.error(`Failed to ${action} instance:`, error)
    } finally {
      setLoading(null)
    }
  }

  return (
    <div className="flex gap-1">
      {currentStatus === 'stopped' && (
        <button
          className="text-green-600 hover:underline text-sm"
          onClick={() => performAction('start')}
          disabled={loading !== null}
        >
          {loading === 'start' ? '...' : 'Start'}
        </button>
      )}

      {currentStatus === 'running' && (
        <button
          className="text-yellow-600 hover:underline text-sm"
          onClick={() => performAction('stop')}
          disabled={loading !== null}
        >
          {loading === 'stop' ? '...' : 'Stop'}
        </button>
      )}

      {currentStatus === 'running' && (
        <button
          className="text-blue-600 hover:underline text-sm"
          onClick={() => performAction('restart')}
          disabled={loading !== null}
        >
          {loading === 'restart' ? '...' : 'Restart'}
        </button>
      )}

      <button
        className="text-red-600 hover:underline text-sm"
        onClick={() => {
          if (confirm(`Uninstall instance ${instanceId}?`)) {
            performAction('uninstall')
          }
        }}
        disabled={loading !== null}
      >
        {loading === 'uninstall' ? '...' : 'Uninstall'}
      </button>
    </div>
  )
}
