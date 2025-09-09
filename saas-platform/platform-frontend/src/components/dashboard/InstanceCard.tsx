import { ExternalLink, CheckCircle, AlertCircle, Loader2, XCircle, Rocket, Copy } from 'lucide-react'
import Link from 'next/link'
import { useState } from 'react'
import type { Instance } from '@/hooks/useInstance'
import { provisionInstance } from '@/lib/api'

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
      await provisionInstance()
      // Refresh the page to show the new instance
      window.location.reload()
    } catch (error: any) {
      alert(`Failed to provision instance: ${error.message || 'Unknown error'}`)
    } finally {
      setIsProvisioning(false)
    }
  }

  // Token passing handled by shared SSO cookie; open via plain link

  // No instance yet - show provision card
  if (!instance) {
    return (
      <div className="bg-white rounded-lg shadow-md p-6">
        <h2 className="text-xl font-bold mb-4">MindRoom Instance</h2>
        <div className="text-center py-8">
          <Rocket className="w-16 h-16 text-gray-400 mx-auto mb-4" />
          <p className="text-gray-600 mb-6">
            No instance provisioned yet. Click below to create your MindRoom instance.
          </p>
          <button
            onClick={handleProvision}
            disabled={isProvisioning}
            className="px-6 py-3 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors disabled:bg-gray-400 disabled:cursor-not-allowed"
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
      </div>
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
        return 'text-green-600 bg-green-50'
      case 'provisioning':
        return 'text-blue-600 bg-blue-50'
      case 'stopped':
        return 'text-yellow-600 bg-yellow-50'
      case 'error':
      case 'failed':
        return 'text-red-600 bg-red-50'
      default:
        return 'text-gray-600 bg-gray-50'
    }
  }

  const frontendHost = getHostname(instance.frontend_url)
  const backendHost = getHostname(instance.backend_url)
  const matrixHost = getHostname(instance.matrix_server_url)

  return (
    <div className="bg-white rounded-lg shadow-md p-6">
      <div className="flex justify-between items-start mb-4">
        <h2 className="text-xl font-bold">MindRoom Instance</h2>
        <div className={`flex items-center gap-2 px-3 py-1 rounded-full ${getStatusColor()}`}>
          {getStatusIcon()}
          <span className="text-sm font-medium">{getStatusText()}</span>
        </div>
      </div>

      <div className="space-y-3">
        {/* Domain */}
        {frontendHost && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600">Domain</span>
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm">{frontendHost}</span>
              <button
                onClick={() => copyToClipboard(frontendHost)}
                title="Copy domain"
                className="text-gray-500 hover:text-gray-700"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Frontend URL */}
        {instance.frontend_url && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600">Frontend</span>
            <div className="flex items-center gap-2">
              <Link
                href={instance.frontend_url}
                target="_blank"
                className="flex items-center gap-1 text-blue-600 hover:text-blue-700 font-medium"
              >
                {frontendHost || 'Open'}
                <ExternalLink className="w-3 h-3" />
              </Link>
              <button
                onClick={() => copyToClipboard(instance.frontend_url!)}
                title="Copy URL"
                className="text-gray-500 hover:text-gray-700"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Backend API */}
        {instance.backend_url && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600">API</span>
            <div className="flex items-center gap-2">
              <Link
                href={instance.backend_url}
                target="_blank"
                className="flex items-center gap-1 text-blue-600 hover:text-blue-700 font-medium"
              >
                {backendHost || 'Open'}
                <ExternalLink className="w-3 h-3" />
              </Link>
              <button
                onClick={() => copyToClipboard(instance.backend_url!)}
                title="Copy API URL"
                className="text-gray-500 hover:text-gray-700"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Tier */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Tier</span>
          <span className="font-medium capitalize">{instance.tier || 'Free'}</span>
        </div>

        {/* Matrix Server */}
        {instance.matrix_server_url && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600">Matrix Server</span>
            <div className="flex items-center gap-2">
              <Link
                href={instance.matrix_server_url}
                target="_blank"
                className="flex items-center gap-1 text-purple-600 hover:text-purple-700 font-medium"
              >
                {matrixHost || 'Connect'}
                <ExternalLink className="w-3 h-3" />
              </Link>
              <button
                onClick={() => copyToClipboard(instance.matrix_server_url!)}
                title="Copy Matrix URL"
                className="text-gray-500 hover:text-gray-700"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}

        {/* Last Updated */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Last Updated</span>
          <span className="text-sm text-gray-500">
            {formatRelativeTime(instance.updated_at)} · {new Date(instance.updated_at).toLocaleString()}
          </span>
        </div>

        {/* Instance ID */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Instance ID</span>
          <span className="font-mono font-medium">#{instance.instance_id}</span>
        </div>
      </div>

      {/* Action Buttons */}
      {instance.status === 'running' && instance.frontend_url && (
        <div className="mt-6 pt-6 border-t">
          <Link
            href={instance.frontend_url}
            target="_blank"
            rel="noopener noreferrer"
            className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors"
          >
            <ExternalLink className="w-4 h-4" />
            Open MindRoom
          </Link>
        </div>
      )}
    </div>
  )
}
