'use client'

import { useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Loader2, RefreshCw, CheckCircle, AlertCircle, Clock, Play, Pause, ExternalLink, Server, Database, Globe } from 'lucide-react'
import { listInstances, startInstance, stopInstance, restartInstance as apiRestartInstance } from '@/lib/api'
import { cache } from '@/lib/cache'
import { getRuntimeConfig } from '@/lib/runtime-config'
import { logger } from '@/lib/logger'
import { Card, CardHeader, CardSection } from '@/components/ui/Card'

type InstanceStatus = 'provisioning' | 'running' | 'stopped' | 'failed' | 'error' | 'deprovisioned' | 'restarting'

type Instance = {
  id: string
  instance_id: number | string
  subscription_id: string
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
  const cachedInstance = cache.get('user-instance') as Instance | null
  const [instance, setInstance] = useState<Instance | null>(cachedInstance)
  const [loading, setLoading] = useState(!cachedInstance)
  const [refreshing, setRefreshing] = useState(false)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const { platformDomain, apiUrl } = useMemo(() => getRuntimeConfig(), [])


  useEffect(() => {
    // Only fetch if no cached data, otherwise fetch silently in background
    if (!cachedInstance) {
      fetchInstance()
    } else {
      // Fetch silently in background to get fresh data
      fetchInstance(true)
    }
  }, []) // Run only on mount

  useEffect(() => {
    // Poll for updates while provisioning or restarting
    if (instance?.status === 'provisioning' || instance?.status === 'restarting') {
      const interval = setInterval(() => {
        fetchInstance(true)
      }, 5000) // Poll every 5 seconds

      return () => clearInterval(interval)
    }
  }, [instance?.status])

  const fetchInstance = async (silent = false) => {
    if (!silent) setLoading(true)

    try {
      const data = await listInstances()

      if (data.instances && data.instances.length > 0) {
        const newInstance = data.instances[0]
        setInstance(newInstance)
        cache.set('user-instance', newInstance)
      } else {
        setInstance(null)
      }
    } catch (error) {
      logger.error('Error fetching instance:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleRefresh = async () => {
    setRefreshing(true)
    await fetchInstance()
    setRefreshing(false)
  }

  const handleAction = async (action: 'start' | 'stop' | 'restart' | 'delete' | 'reprovision') => {
    if (!instance) return

    setActionLoading(action)

    try {
      switch (action) {
        case 'start':
          await startInstance(instance.instance_id)
          break
        case 'stop':
          await stopInstance(instance.instance_id)
          break
        case 'restart':
          await apiRestartInstance(instance.instance_id)
          break
        case 'reprovision':
          // Use the provision endpoint which now handles reprovisioning
          const { provisionInstance } = await import('@/lib/api')
          await provisionInstance()
          break
        case 'delete':
          // Delete not implemented yet
          throw new Error('Delete not implemented')
      }

      // Refresh instance status
      await fetchInstance()
    } catch (error: any) {
      // Don't show error for cancelled requests (user navigated away/refreshed)
      // Check for various abort/cancel conditions
      const isAborted =
        error?.name === 'AbortError' ||
        error?.message?.includes('aborted') ||
        error?.message?.includes('cancelled') ||
        error?.message?.includes('Failed to fetch') ||
        error?.code === 'ECONNABORTED' ||
        error?.code === 20 || // Chrome abort code
        !error?.message || // Empty errors often indicate cancellation
        error?.message === ''

      if (!isAborted) {
        logger.error(`Error performing ${action}:`, error)
        alert(`Failed to ${action} instance. Please try again.`)
      }
    } finally {
      setActionLoading(null)
    }
  }

  const getStatusIcon = (status: InstanceStatus) => {
    switch (status) {
      case 'running':
        return <CheckCircle className="w-5 h-5 text-green-500" />
      case 'provisioning':
      case 'restarting':
        return <Loader2 className="w-5 h-5 text-orange-500 animate-spin" />
      case 'stopped':
      case 'deprovisioned':
        return <Clock className="w-5 h-5 text-yellow-500" />
      case 'failed':
      case 'error':
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
      case 'restarting':
        return 'Restarting your instance... This will take a moment.'
      case 'stopped':
        return 'Instance is stopped. Start it to access your MindRoom.'
      case 'failed':
        return 'Instance provisioning failed. Please contact support.'
      case 'error':
        return 'Instance not found in cluster. It may have been removed during maintenance. Please contact support to reprovision your instance.'
      case 'deprovisioned':
        return 'Instance has been removed. Click "Reprovision Instance" to restore it.'
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
      <div className="max-w-4xl mx-auto">
        <Card padding="xl" className="text-center">
          <Server className="w-20 h-20 text-gray-400 dark:text-gray-500 mx-auto mb-6" />
          <CardHeader className="mb-3">No Instance Found</CardHeader>
          <p className="text-gray-600 dark:text-gray-400 mb-8 text-lg">
            You don't have a MindRoom instance yet. Upgrade to a paid plan to get your own instance.
          </p>
          <button
            onClick={() => router.push('/dashboard/billing/upgrade')}
            className="px-8 py-3 bg-gradient-to-r from-orange-500 to-orange-600 text-white rounded-xl font-semibold hover:shadow-lg hover:scale-105 transition-all"
          >
            Upgrade Plan
          </button>
        </Card>
      </div>
    )
  }

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <h1 className="text-3xl font-bold bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">Your MindRoom Instance</h1>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="flex items-center justify-center gap-2 px-5 py-2.5 border border-gray-300 dark:border-gray-600 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50 transition-all font-medium"
        >
          <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Status Card */}
      <Card>
        <div className="flex items-start justify-between mb-6">
          <div>
            <h2 className="text-xl font-bold mb-2">Instance Status</h2>
            <div className="flex items-center gap-2">
              {getStatusIcon(instance.status)}
              <span className={`
                font-medium capitalize
                ${instance.status === 'running' ? 'text-green-600' : ''}
                ${instance.status === 'provisioning' ? 'text-orange-600' : ''}
                ${instance.status === 'restarting' ? 'text-orange-600' : ''}
                ${instance.status === 'stopped' ? 'text-yellow-600' : ''}
                ${instance.status === 'deprovisioned' ? 'text-gray-600' : ''}
                ${instance.status === 'failed' ? 'text-red-600' : ''}
                ${instance.status === 'error' ? 'text-red-600' : ''}
              `}>
                {instance.status}
              </span>
            </div>
            <p className="text-sm text-gray-600 mt-2">
              {getStatusText(instance.status)}
            </p>
          </div>

          {/* Action Buttons */}
          {instance.status === 'restarting' && (
            <div className="flex items-center gap-2 text-orange-600">
              <Loader2 className="w-4 h-4 animate-spin" />
              <span className="text-sm font-medium">Restarting...</span>
            </div>
          )}

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

          {instance.status === 'deprovisioned' && (
            <button
              onClick={() => handleAction('reprovision')}
              disabled={actionLoading !== null}
              className="flex items-center gap-2 px-4 py-2 bg-orange-600 text-white rounded-lg hover:bg-orange-700 disabled:opacity-50 transition-colors"
            >
              {actionLoading === 'reprovision' ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <RefreshCw className="w-4 h-4" />
              )}
              Reprovision Instance
            </button>
          )}

          {instance.status === 'error' && (
            <div className="flex gap-2">
              <button
                onClick={() => window.location.href = 'mailto:support@mindroom.chat?subject=Instance Error - Reprovision Request&body=My instance ID: ' + String(instance.instance_id) + ' (subdomain: ' + instance.subdomain + ') is showing an error status and needs to be reprovisioned.'}
                className="flex items-center gap-2 px-4 py-2 bg-orange-600 text-white rounded-lg hover:bg-orange-700 transition-colors"
              >
                <AlertCircle className="w-4 h-4" />
                Contact Support
              </button>
              <button
                onClick={handleRefresh}
                disabled={refreshing}
                className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50 transition-colors"
              >
                <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
                Retry
              </button>
            </div>
          )}
        </div>

        {/* Instance Details */}
        <CardSection>
          <h3 className="font-semibold mb-4">Instance Details</h3>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <p className="text-sm text-gray-600 dark:text-gray-400">Subdomain</p>
              <p className="font-mono text-sm">
                {platformDomain
                  ? `${instance.subdomain}.${platformDomain}`
                  : instance.frontend_url || instance.backend_url || instance.subdomain}
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-600 dark:text-gray-400">Created</p>
              <p className="text-sm">{new Date(instance.created_at).toLocaleString()}</p>
            </div>
            <div>
              <p className="text-sm text-gray-600 dark:text-gray-400">Last Updated</p>
              <p className="text-sm">{new Date(instance.updated_at).toLocaleString()}</p>
            </div>
          </div>
        </CardSection>
      </Card>

      {/* Access URLs (only show when running) */}
      {instance.status === 'running' && instance.frontend_url && (
        <Card>
          <CardHeader className="mb-6">Access Your MindRoom</CardHeader>
          <div className="space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 p-5 bg-gradient-to-r from-orange-50 to-yellow-50 dark:from-orange-900/10 dark:to-yellow-900/10 rounded-2xl border border-orange-200/50 dark:border-orange-800/30">
              <div className="flex items-center gap-3">
                <Globe className="w-5 h-5 text-orange-600 dark:text-orange-400" />
                <div>
                  <p className="font-medium dark:text-white">MindRoom App</p>
                  <p className="text-sm text-gray-600 dark:text-gray-400 break-all">{instance.frontend_url}</p>
                </div>
              </div>
              <a
                href={instance.frontend_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center justify-center gap-2 px-6 py-2.5 bg-gradient-to-r from-orange-500 to-orange-600 text-white rounded-xl font-semibold hover:shadow-lg hover:scale-105 transition-all whitespace-nowrap"
              >
                Open MindRoom
                <ExternalLink className="w-4 h-4" />
              </a>
            </div>

            {instance.backend_url && (
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 p-5 bg-gray-50 dark:bg-gray-700 rounded-2xl">
                <div className="flex items-center gap-3">
                  <Server className="w-5 h-5 text-gray-600 dark:text-gray-400" />
                  <div>
                    <p className="font-medium dark:text-white">API Endpoint</p>
                    <p className="text-sm text-gray-600 dark:text-gray-400">{instance.backend_url}</p>
                  </div>
                </div>
                <a
                  href={`${instance.backend_url}/docs`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-2 px-4 py-2 border border-gray-300 dark:border-gray-600 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-600 transition-colors"
                >
                  API Docs
                  <ExternalLink className="w-4 h-4" />
                </a>
              </div>
            )}

            {instance.matrix_server_url && (
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 p-5 bg-gray-50 dark:bg-gray-700 rounded-2xl">
                <div className="flex items-center gap-3">
                  <Database className="w-5 h-5 text-gray-600 dark:text-gray-400" />
                  <div>
                    <p className="font-medium dark:text-white">Matrix Server</p>
                    <p className="text-sm text-gray-600 dark:text-gray-400">{instance.matrix_server_url}</p>
                  </div>
                </div>
              </div>
            )}
          </div>
        </Card>
      )}

      {/* Configuration (collapsible) */}
      <Card className="overflow-hidden p-0">
        <details>
        <summary className="p-8 cursor-pointer hover:bg-gray-50/50 dark:hover:bg-gray-700/50 transition-colors">
          <CardHeader>Instance Configuration</CardHeader>
        </summary>
        <div className="px-8 pb-8">
          <pre className="bg-gray-50 dark:bg-gray-900/50 p-6 rounded-2xl overflow-x-auto text-sm dark:text-gray-300 font-mono border border-gray-200 dark:border-gray-700">
            {JSON.stringify(instance.config, null, 2)}
          </pre>
        </div>
      </details>
      </Card>
    </div>
  )
}
