'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { Loader2, RefreshCw, CheckCircle, AlertCircle, Clock, Play, Pause, Trash2, ExternalLink, Server, Database, Globe } from 'lucide-react'
import { createClient } from '@/lib/supabase/client'

type InstanceStatus = 'provisioning' | 'running' | 'stopped' | 'failed' | 'deprovisioning' | 'maintenance'

type Instance = {
  id: string
  subscription_id: string
  dokku_app_name: string
  subdomain: string
  status: InstanceStatus
  backend_url: string | null
  frontend_url: string | null
  matrix_server_url: string | null
  config: any
  created_at: string
  updated_at: string
}

export default function InstancePage() {
  const router = useRouter()
  const [instance, setInstance] = useState<Instance | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [actionLoading, setActionLoading] = useState<string | null>(null)

  useEffect(() => {
    fetchInstance()
    // Poll for updates while provisioning
    const interval = setInterval(() => {
      if (instance?.status === 'provisioning') {
        fetchInstance(true)
      }
    }, 5000) // Poll every 5 seconds

    return () => clearInterval(interval)
  }, [instance?.status])

  const fetchInstance = async (silent = false) => {
    if (!silent) setLoading(true)

    try {
      const supabase = createClient()

      // Get current user
      const { data: { user } } = await supabase.auth.getUser()
      if (!user) {
        router.push('/auth/login')
        return
      }

      // Get user's subscription
      const { data: subscription } = await supabase
        .from('subscriptions')
        .select('*')
        .eq('account_id', user.id)
        .single()

      if (!subscription) {
        setLoading(false)
        return
      }

      // Get instance for subscription
      const { data: instanceData } = await supabase
        .from('instances')
        .select('*')
        .eq('subscription_id', subscription.id)
        .single()

      setInstance(instanceData)
    } catch (error) {
      console.error('Error fetching instance:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleRefresh = async () => {
    setRefreshing(true)
    await fetchInstance()
    setRefreshing(false)
  }

  const handleAction = async (action: 'start' | 'stop' | 'restart' | 'delete') => {
    if (!instance) return

    setActionLoading(action)

    try {
      const response = await fetch('/api/instance/' + action, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          instanceId: instance.id,
        }),
      })

      if (!response.ok) {
        throw new Error('Action failed')
      }

      // Refresh instance status
      await fetchInstance()
    } catch (error) {
      console.error(`Error performing ${action}:`, error)
      alert(`Failed to ${action} instance. Please try again.`)
    } finally {
      setActionLoading(null)
    }
  }

  const getStatusIcon = (status: InstanceStatus) => {
    switch (status) {
      case 'running':
        return <CheckCircle className="w-5 h-5 text-green-500" />
      case 'provisioning':
      case 'deprovisioning':
        return <Loader2 className="w-5 h-5 text-orange-500 animate-spin" />
      case 'stopped':
      case 'maintenance':
        return <Clock className="w-5 h-5 text-yellow-500" />
      case 'failed':
        return <AlertCircle className="w-5 h-5 text-red-500" />
      default:
        return null
    }
  }

  const getStatusText = (status: InstanceStatus) => {
    switch (status) {
      case 'running':
        return 'Instance is running and accessible'
      case 'provisioning':
        return 'Setting up your MindRoom instance... This may take a few minutes.'
      case 'stopped':
        return 'Instance is stopped. Start it to access your MindRoom.'
      case 'failed':
        return 'Instance provisioning failed. Please contact support.'
      case 'deprovisioning':
        return 'Removing instance...'
      case 'maintenance':
        return 'Instance is under maintenance. It will be back soon.'
      default:
        return 'Unknown status'
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 className="w-8 h-8 animate-spin text-orange-500" />
      </div>
    )
  }

  if (!instance) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <div className="bg-white rounded-lg p-8 text-center">
          <Server className="w-16 h-16 text-gray-400 mx-auto mb-4" />
          <h2 className="text-2xl font-bold mb-2">No Instance Found</h2>
          <p className="text-gray-600 mb-6">
            You don't have a MindRoom instance yet. Upgrade to a paid plan to get your own instance.
          </p>
          <button
            onClick={() => router.push('/dashboard/billing/upgrade')}
            className="px-6 py-3 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors"
          >
            Upgrade Plan
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold">Your MindRoom Instance</h1>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50 transition-colors"
        >
          <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* Status Card */}
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <div className="flex items-start justify-between mb-6">
          <div>
            <h2 className="text-xl font-bold mb-2">Instance Status</h2>
            <div className="flex items-center gap-2">
              {getStatusIcon(instance.status)}
              <span className={`
                font-medium capitalize
                ${instance.status === 'running' ? 'text-green-600' : ''}
                ${instance.status === 'provisioning' ? 'text-orange-600' : ''}
                ${instance.status === 'stopped' ? 'text-yellow-600' : ''}
                ${instance.status === 'failed' ? 'text-red-600' : ''}
              `}>
                {instance.status}
              </span>
            </div>
            <p className="text-sm text-gray-600 mt-2">
              {getStatusText(instance.status)}
            </p>
          </div>

          {/* Action Buttons */}
          {instance.status === 'running' && (
            <div className="flex gap-2">
              <button
                onClick={() => handleAction('restart')}
                disabled={actionLoading !== null}
                className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50 transition-colors"
              >
                {actionLoading === 'restart' ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
                Restart
              </button>
              <button
                onClick={() => handleAction('stop')}
                disabled={actionLoading !== null}
                className="flex items-center gap-2 px-4 py-2 border border-red-300 text-red-600 rounded-lg hover:bg-red-50 disabled:opacity-50 transition-colors"
              >
                {actionLoading === 'stop' ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Pause className="w-4 h-4" />
                )}
                Stop
              </button>
            </div>
          )}

          {instance.status === 'stopped' && (
            <button
              onClick={() => handleAction('start')}
              disabled={actionLoading !== null}
              className="flex items-center gap-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 transition-colors"
            >
              {actionLoading === 'start' ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Play className="w-4 h-4" />
              )}
              Start Instance
            </button>
          )}
        </div>

        {/* Instance Details */}
        <div className="border-t pt-6">
          <h3 className="font-semibold mb-4">Instance Details</h3>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <p className="text-sm text-gray-600">App Name</p>
              <p className="font-mono text-sm">{instance.dokku_app_name}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600">Subdomain</p>
              <p className="font-mono text-sm">{instance.subdomain}.staging.mindroom.chat</p>
            </div>
            <div>
              <p className="text-sm text-gray-600">Created</p>
              <p className="text-sm">{new Date(instance.created_at).toLocaleString()}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600">Last Updated</p>
              <p className="text-sm">{new Date(instance.updated_at).toLocaleString()}</p>
            </div>
          </div>
        </div>
      </div>

      {/* Access URLs (only show when running) */}
      {instance.status === 'running' && instance.frontend_url && (
        <div className="bg-white rounded-lg p-6 shadow-sm">
          <h2 className="text-xl font-bold mb-4">Access Your MindRoom</h2>
          <div className="space-y-4">
            <div className="flex items-center justify-between p-4 bg-gray-50 rounded-lg">
              <div className="flex items-center gap-3">
                <Globe className="w-5 h-5 text-gray-600" />
                <div>
                  <p className="font-medium">MindRoom App</p>
                  <p className="text-sm text-gray-600">{instance.frontend_url}</p>
                </div>
              </div>
              <a
                href={instance.frontend_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 px-4 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors"
              >
                Open MindRoom
                <ExternalLink className="w-4 h-4" />
              </a>
            </div>

            {instance.backend_url && (
              <div className="flex items-center justify-between p-4 bg-gray-50 rounded-lg">
                <div className="flex items-center gap-3">
                  <Server className="w-5 h-5 text-gray-600" />
                  <div>
                    <p className="font-medium">API Endpoint</p>
                    <p className="text-sm text-gray-600">{instance.backend_url}</p>
                  </div>
                </div>
                <a
                  href={`${instance.backend_url}/docs`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
                >
                  API Docs
                  <ExternalLink className="w-4 h-4" />
                </a>
              </div>
            )}

            {instance.matrix_server_url && (
              <div className="flex items-center justify-between p-4 bg-gray-50 rounded-lg">
                <div className="flex items-center gap-3">
                  <Database className="w-5 h-5 text-gray-600" />
                  <div>
                    <p className="font-medium">Matrix Server</p>
                    <p className="text-sm text-gray-600">{instance.matrix_server_url}</p>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Configuration (collapsible) */}
      <details className="bg-white rounded-lg shadow-sm">
        <summary className="p-6 cursor-pointer hover:bg-gray-50 transition-colors">
          <span className="font-bold text-xl">Instance Configuration</span>
        </summary>
        <div className="px-6 pb-6">
          <pre className="bg-gray-50 p-4 rounded-lg overflow-x-auto text-sm">
            {JSON.stringify(instance.config, null, 2)}
          </pre>
        </div>
      </details>
    </div>
  )
}
