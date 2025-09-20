import { ExternalLink, CheckCircle, AlertCircle, Loader2, XCircle, Rocket, Copy } from 'lucide-react'
import Link from 'next/link'
import { useState } from 'react'
import type { Instance } from '@/hooks/useInstance'
import { provisionInstance } from '@/lib/api'
import { Card, CardHeader } from '@/components/ui/Card'
import { logger } from '@/lib/logger'

export function InstanceCard({ instance }: { instance: Instance | null }) {
  const [isProvisioning, setIsProvisioning] = useState(false)
  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // non-fatal
    }
  }

  const getHostname = (url?: string | null) => {
    if (!url) return null
    try {
      const u = new URL(url)
      return u.hostname
    } catch {
      return null
    }
  }

  const formatRelativeTime = (dateStr?: string) => {
    if (!dateStr) return '—'
    const date = new Date(dateStr)
    const diff = Date.now() - date.getTime()
    const seconds = Math.floor(diff / 1000)
    const minutes = Math.floor(seconds / 60)
    const hours = Math.floor(minutes / 60)
    const days = Math.floor(hours / 24)
    if (days > 0) return `${days}d ago`
    if (hours > 0) return `${hours}h ago`
    if (minutes > 0) return `${minutes}m ago`
    return 'just now'
  }

  const handleProvision = async () => {
    setIsProvisioning(true)
    try {
      const result = await provisionInstance()
      logger.log('Provision result:', result)
      // Refresh the page to show the new instance
      window.location.reload()
    } catch (error: any) {
      // Don't show error for cancelled requests (user navigated away/refreshed)
      const isAborted =
        error?.name === 'AbortError' ||
        error?.message?.includes('aborted') ||
        error?.message?.includes('cancelled') ||
        error?.message?.includes('Failed to fetch') ||
        !error?.message ||
        error?.message === ''

      if (isAborted) {
        return
      }

      logger.error('Provision error:', error)
      const errorMessage = error.message || 'Unknown error'
      // Check for specific error conditions
      if (errorMessage.includes('No subscription found')) {
        alert('Please wait for your account setup to complete, then try again.')
      } else {
        alert(`Failed to provision instance: ${errorMessage}`)
      }
    } finally {
      setIsProvisioning(false)
    }
  }

  // Token passing handled by shared SSO cookie; open via plain link

  // No instance yet - show provision card
  if (!instance) {
    return (
      <Card>
        <CardHeader>MindRoom Instance</CardHeader>
        <div className="text-center py-8">
          <Rocket className="w-16 h-16 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
          <p className="text-gray-600 dark:text-gray-400 mb-6">
            No instance provisioned yet. Click below to create your MindRoom instance.
          </p>
          <button
            onClick={handleProvision}
            disabled={isProvisioning}
            className="px-6 py-3 bg-gradient-to-r from-orange-500 to-orange-600 text-white rounded-xl font-semibold hover:shadow-lg hover:scale-105 transition-all disabled:bg-gray-400 disabled:cursor-not-allowed"
          >
            {isProvisioning ? (
              <>
                <Loader2 className="inline-block w-5 h-5 mr-2 animate-spin" />
                Provisioning...
              </>
            ) : (
              'Provision Instance'
            )}
          </button>
        </div>
      </Card>
    )
  }

  const getStatusIcon = () => {
    switch (instance.status) {
      case 'running':
        return <CheckCircle className="w-5 h-5 text-green-500" />
      case 'provisioning':
        return <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />
      case 'stopped':
        return <AlertCircle className="w-5 h-5 text-yellow-500" />
      case 'error':
      case 'failed':
        return <XCircle className="w-5 h-5 text-red-500" />
      default:
        return <AlertCircle className="w-5 h-5 text-gray-500" />
    }
  }

  const getStatusText = () => {
    switch (instance.status) {
      case 'running':
        return 'Running'
      case 'provisioning':
        return 'Provisioning...'
      case 'stopped':
        return 'Stopped'
      case 'error':
      case 'failed':
        return 'Error'
      default:
        return instance.status
    }
  }

  const getStatusColor = () => {
    switch (instance.status) {
      case 'running':
        return 'text-green-600 bg-green-50 dark:text-green-400 dark:bg-green-900/20'
      case 'provisioning':
        return 'text-blue-600 bg-blue-50 dark:text-blue-400 dark:bg-blue-900/20'
      case 'stopped':
        return 'text-yellow-600 bg-yellow-50 dark:text-yellow-400 dark:bg-yellow-900/20'
      case 'error':
      case 'failed':
        return 'text-red-600 bg-red-50 dark:text-red-400 dark:bg-red-900/20'
      default:
        return 'text-gray-600 bg-gray-50 dark:text-gray-400 dark:bg-gray-800'
    }
  }

  const frontendHost = getHostname(instance.frontend_url)
  const backendHost = getHostname(instance.backend_url)
  const matrixHost = getHostname(instance.matrix_server_url)
  const lastSynced = instance.kubernetes_synced_at
    ? formatRelativeTime(instance.kubernetes_synced_at)
    : null
  const statusHint = instance.status_hint
    || (instance.status === 'provisioning'
      ? 'Instance provisioning in progress. First boot can take a few minutes while containers pull and TLS certificates issue.'
      : instance.status === 'restarting'
        ? 'Instance is restarting and will be reachable again soon.'
        : null)

  return (
    <Card>
      <div className="flex justify-between items-start mb-4">
        <CardHeader>MindRoom Instance</CardHeader>
        <div className={`flex items-center gap-2 px-3 py-1 rounded-full ${getStatusColor()}`}>
          {getStatusIcon()}
          <span className="text-sm font-medium">{getStatusText()}</span>
        </div>
      </div>

      {(statusHint || lastSynced) && (
        <div className="mb-3 space-y-1">
          {statusHint && (
            <p className="text-sm text-gray-600 dark:text-gray-400 leading-relaxed">
              {statusHint}
            </p>
          )}
          {lastSynced && (
            <p className="text-xs text-gray-500 dark:text-gray-500">
              Last checked {lastSynced}.
            </p>
          )}
        </div>
      )}

      <div className="space-y-3">
        {/* Domain */}
        {frontendHost && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600 dark:text-gray-400">Domain</span>
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm dark:text-gray-300">{frontendHost}</span>
              <button
                onClick={() => copyToClipboard(frontendHost)}
                title="Copy domain"
                className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Frontend URL */}
        {instance.frontend_url && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600 dark:text-gray-400">Frontend</span>
            <div className="flex items-center gap-2">
              <Link
                href={instance.frontend_url}
                target="_blank"
                className="flex items-center gap-1 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 font-medium"
              >
                {frontendHost || 'Open'}
                <ExternalLink className="w-3 h-3" />
              </Link>
              <button
                onClick={() => copyToClipboard(instance.frontend_url!)}
                title="Copy URL"
                className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Backend API */}
        {instance.backend_url && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600 dark:text-gray-400">API</span>
            <div className="flex items-center gap-2">
              <Link
                href={instance.backend_url}
                target="_blank"
                className="flex items-center gap-1 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 font-medium"
              >
                {backendHost || 'Open'}
                <ExternalLink className="w-3 h-3" />
              </Link>
              <button
                onClick={() => copyToClipboard(instance.backend_url!)}
                title="Copy API URL"
                className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Tier */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600 dark:text-gray-400">Tier</span>
          <span className="font-medium capitalize dark:text-gray-200">{instance.tier || 'Free'}</span>
        </div>

        {/* Matrix Server */}
        {instance.matrix_server_url && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600 dark:text-gray-400">Matrix Server</span>
            <div className="flex items-center gap-2">
              <Link
                href={instance.matrix_server_url}
                target="_blank"
                className="flex items-center gap-1 text-purple-600 hover:text-purple-700 dark:text-purple-400 dark:hover:text-purple-300 font-medium"
              >
                {matrixHost || 'Connect'}
                <ExternalLink className="w-3 h-3" />
              </Link>
              <button
                onClick={() => copyToClipboard(instance.matrix_server_url!)}
                title="Copy Matrix URL"
                className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Last Updated */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600 dark:text-gray-400">Last Updated</span>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            {formatRelativeTime(instance.updated_at)} · {new Date(instance.updated_at).toLocaleString()}
          </span>
        </div>

        {/* Instance ID */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600 dark:text-gray-400">Instance ID</span>
          <span className="font-mono font-medium dark:text-gray-200">#{instance.instance_id}</span>
        </div>
      </div>

      {/* Action Buttons */}
      {instance.status === 'running' && instance.frontend_url && (
        <div className="mt-6 pt-6 border-t dark:border-gray-700">
          <Link
            href={instance.frontend_url}
            target="_blank"
            rel="noopener noreferrer"
            className="w-full inline-flex items-center justify-center gap-2 px-6 py-3 bg-gradient-to-r from-orange-500 to-orange-600 text-white rounded-xl font-semibold hover:shadow-lg hover:scale-105 transition-all"
          >
            <ExternalLink className="w-4 h-4" />
            Open MindRoom
          </Link>
        </div>
      )}
    </Card>
  )
}
