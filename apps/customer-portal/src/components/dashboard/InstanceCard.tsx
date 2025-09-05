import { ExternalLink, CheckCircle, AlertCircle, Loader2, XCircle } from 'lucide-react'
import Link from 'next/link'
import type { Instance } from '@/hooks/useInstance'

export function InstanceCard({ instance }: { instance: Instance | null }) {
  const getStatusIcon = () => {
    switch (instance?.status) {
      case 'running':
        return <CheckCircle className="w-5 h-5 text-green-500" />
      case 'provisioning':
        return <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />
      case 'failed':
        return <XCircle className="w-5 h-5 text-red-500" />
      case 'stopped':
        return <AlertCircle className="w-5 h-5 text-gray-400" />
      default:
        return <AlertCircle className="w-5 h-5 text-gray-400" />
    }
  }

  const getStatusText = () => {
    switch (instance?.status) {
      case 'running':
        return 'Running'
      case 'provisioning':
        return 'Setting up your MindRoom...'
      case 'failed':
        return 'Setup failed - Please contact support'
      case 'stopped':
        return 'Stopped'
      default:
        return 'Unknown'
    }
  }

  const getStatusColor = () => {
    switch (instance?.status) {
      case 'running':
        return 'text-green-700 bg-green-50'
      case 'provisioning':
        return 'text-blue-700 bg-blue-50'
      case 'failed':
        return 'text-red-700 bg-red-50'
      case 'stopped':
        return 'text-gray-700 bg-gray-50'
      default:
        return 'text-gray-700 bg-gray-50'
    }
  }

  if (!instance) {
    return (
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4">Your MindRoom Instance</h2>
        <div className="text-center py-8">
          <Loader2 className="w-8 h-8 mx-auto text-gray-400 animate-spin" />
          <p className="text-gray-500 mt-4">Setting up your instance...</p>
          <p className="text-sm text-gray-400 mt-2">This may take a few minutes</p>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-white rounded-lg p-6 shadow-sm">
      <h2 className="text-xl font-bold mb-4">Your MindRoom Instance</h2>

      <div className="space-y-4">
        {/* Status */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Status</span>
          <div className="flex items-center gap-2">
            {getStatusIcon()}
            <span className={`font-medium px-2 py-1 rounded-full text-sm ${getStatusColor()}`}>
              {getStatusText()}
            </span>
          </div>
        </div>

        {/* URL */}
        {instance.frontend_url && instance.status === 'running' && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600">URL</span>
            <Link
              href={instance.frontend_url}
              target="_blank"
              className="flex items-center gap-1 text-blue-600 hover:text-blue-700 font-medium"
            >
              Open MindRoom
              <ExternalLink className="w-4 h-4" />
            </Link>
          </div>
        )}

        {/* Subdomain */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Subdomain</span>
          <span className="font-mono text-sm bg-gray-100 px-2 py-1 rounded">
            {instance.subdomain}.mindroom.app
          </span>
        </div>

        {/* Created */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Created</span>
          <span className="text-sm">
            {new Date(instance.created_at).toLocaleDateString()}
          </span>
        </div>

        {/* Last Updated */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Last Updated</span>
          <span className="text-sm">
            {new Date(instance.updated_at).toLocaleString()}
          </span>
        </div>
      </div>

      {/* Action Buttons */}
      {instance.status === 'running' && (
        <div className="mt-6 pt-6 border-t">
          <Link
            href={instance.frontend_url || '#'}
            target="_blank"
            className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors"
          >
            <ExternalLink className="w-4 h-4" />
            Open MindRoom
          </Link>
        </div>
      )}
    </div>
  )
}
