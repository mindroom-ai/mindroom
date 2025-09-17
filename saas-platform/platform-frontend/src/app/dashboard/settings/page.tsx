'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import { Card, CardHeader, CardSection } from '@/components/ui/Card'
import {
  Download,
  Trash2,
  Shield,
  AlertTriangle,
  CheckCircle,
  Loader2,
  XCircle,
  Info
} from 'lucide-react'
import {
  exportUserData,
  requestAccountDeletion,
  cancelAccountDeletion,
  updateConsent,
  getAccount
} from '@/lib/api'
import { logger } from '@/lib/logger'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase/client'

// Debounce hook for preventing rapid API calls
function useDebounce<T extends (...args: any[]) => any>(
  callback: T,
  delay: number
): (...args: Parameters<T>) => void {
  const timeoutRef = useRef<NodeJS.Timeout | null>(null)

  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
    }
  }, [])

  return useCallback((...args: Parameters<T>) => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current)
    }
    timeoutRef.current = setTimeout(() => {
      callback(...args)
    }, delay)
  }, [callback, delay])
}

export default function SettingsPage() {
  const router = useRouter()
  const [loading, setLoading] = useState<string | null>(null)
  const [message, setMessage] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [consentSettings, setConsentSettings] = useState({
    marketing: false,
    analytics: false
  })
  const [accountInfo, setAccountInfo] = useState<any>(null)
  const [isDeletionPending, setIsDeletionPending] = useState(false)
  const deletionTimeoutRef = useRef<NodeJS.Timeout | null>(null)
  const supabaseClientRef = useRef<ReturnType<typeof createClient> | null>(null)

  useEffect(() => {
    // Create Supabase client once
    supabaseClientRef.current = createClient()
    loadAccountInfo()

    // Cleanup on unmount
    return () => {
      if (deletionTimeoutRef.current) {
        clearTimeout(deletionTimeoutRef.current)
      }
    }
  }, [])

  const loadAccountInfo = async () => {
    try {
      const account = await getAccount()
      setAccountInfo(account)
      // Check if account is pending deletion
      // Only consider it pending if deleted_at is actually set to a truthy value (not null, undefined, or empty string)
      const isPending = account.status === 'pending_deletion' ||
                       (account.deleted_at && account.deleted_at !== null && account.deleted_at !== '')
      setIsDeletionPending(isPending)
      // Set consent preferences if available
      if (account.consent_marketing !== undefined) {
        setConsentSettings({
          marketing: account.consent_marketing,
          analytics: account.consent_analytics || false
        })
      }
    } catch (error) {
      logger.error('Failed to load account info:', error)
    }
  }

  const handleExportData = async () => {
    setLoading('export')
    setMessage(null)

    try {
      const data = await exportUserData()

      // Create a downloadable JSON file
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `mindroom-data-export-${new Date().toISOString().split('T')[0]}.json`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)

      setMessage({ type: 'success', text: 'Your data has been exported successfully!' })
    } catch (error) {
      logger.error('Export failed:', error)
      // Show more specific error message
      const errorMessage = error instanceof Error ? error.message : 'Unknown error occurred'
      setMessage({
        type: 'error',
        text: `Failed to export data: ${errorMessage}`
      })
    } finally {
      setLoading(null)
    }
  }

  const handleDeleteAccount = async () => {
    if (!showDeleteConfirm) {
      setShowDeleteConfirm(true)
      return
    }

    setLoading('delete')
    setMessage(null)

    try {
      const result = await requestAccountDeletion(true)

      if (result.status === 'deletion_scheduled') {
        setMessage({
          type: 'info',
          text: `Account deletion scheduled. You have ${result.grace_period_days} days to cancel this request.`
        })
        setIsDeletionPending(true)

        // Clear any existing timeout
        if (deletionTimeoutRef.current) {
          clearTimeout(deletionTimeoutRef.current)
        }

        // Sign out after scheduling deletion
        deletionTimeoutRef.current = setTimeout(async () => {
          if (supabaseClientRef.current) {
            await supabaseClientRef.current.auth.signOut()
            router.push('/login')
          }
        }, 3000)
      }
    } catch (error) {
      logger.error('Deletion request failed:', error)
      const errorMessage = error instanceof Error ? error.message : 'Unknown error occurred'
      setMessage({
        type: 'error',
        text: `Failed to request account deletion: ${errorMessage}`
      })
    } finally {
      setLoading(null)
      setShowDeleteConfirm(false)
    }
  }

  const handleCancelDeletion = async () => {
    setLoading('cancel')
    setMessage(null)

    try {
      const result = await cancelAccountDeletion()

      if (result.status === 'success') {
        setMessage({ type: 'success', text: 'Account deletion has been cancelled.' })
        setIsDeletionPending(false)
        await loadAccountInfo()
      }
    } catch (error) {
      logger.error('Cancel deletion failed:', error)
      const errorMessage = error instanceof Error ? error.message : 'Unknown error occurred'
      setMessage({
        type: 'error',
        text: `Failed to cancel deletion: ${errorMessage}`
      })
    } finally {
      setLoading(null)
    }
  }

  // Debounced consent update to prevent rapid API calls
  const debouncedConsentUpdate = useDebounce(async (marketing: boolean, analytics: boolean) => {
    setLoading('consent')
    try {
      await updateConsent(marketing, analytics)
      setMessage({ type: 'success', text: 'Privacy preferences updated.' })
    } catch (error) {
      logger.error('Consent update failed:', error)
      const errorMessage = error instanceof Error ? error.message : 'Unknown error occurred'
      setMessage({
        type: 'error',
        text: `Failed to update preferences: ${errorMessage}`
      })
      // Revert on error - reload account info to get correct state
      await loadAccountInfo()
    } finally {
      setLoading(null)
    }
  }, 500) // 500ms debounce delay

  const handleConsentUpdate = (type: 'marketing' | 'analytics') => {
    // Disable if already loading
    if (loading === 'consent') return

    const newSettings = {
      ...consentSettings,
      [type]: !consentSettings[type]
    }

    // Update UI immediately (optimistic update)
    setConsentSettings(newSettings)

    // Debounced API call
    debouncedConsentUpdate(newSettings.marketing, newSettings.analytics)
  }

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <h1 className="text-3xl font-bold mb-8 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
        Settings
      </h1>

      {message && (
        <div className={`mb-6 p-4 rounded-lg flex items-center gap-3 ${
          message.type === 'success' ? 'bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-200' :
          message.type === 'error' ? 'bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200' :
          'bg-blue-50 dark:bg-blue-900/20 text-blue-800 dark:text-blue-200'
        }`}>
          {message.type === 'success' && <CheckCircle className="h-5 w-5" />}
          {message.type === 'error' && <XCircle className="h-5 w-5" />}
          {message.type === 'info' && <Info className="h-5 w-5" />}
          {message.text}
        </div>
      )}

      {/* Account Deletion Warning */}
      {isDeletionPending && (
        <Card variant="danger" className="mb-6">
          <div className="flex items-start gap-4">
            <AlertTriangle className="h-6 w-6 text-red-600 dark:text-red-400 mt-1" />
            <div className="flex-1">
              <h3 className="font-semibold text-red-900 dark:text-red-100 mb-2">
                Account Deletion Pending
              </h3>
              <p className="text-red-700 dark:text-red-200 text-sm mb-4">
                Your account is scheduled for deletion. All your data will be permanently removed after the grace period.
                You can cancel this request if you change your mind.
              </p>
              <button
                onClick={handleCancelDeletion}
                disabled={loading === 'cancel'}
                className="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
              >
                {loading === 'cancel' ? (
                  <><Loader2 className="h-4 w-4 animate-spin" /> Cancelling...</>
                ) : (
                  'Cancel Deletion Request'
                )}
              </button>
            </div>
          </div>
        </Card>
      )}

      {/* Privacy & Data */}
      <Card className="mb-6">
        <CardHeader>Privacy & Data</CardHeader>

        <div className="mt-6">
          <h3 className="font-semibold text-gray-900 dark:text-white mb-4">Data Export</h3>
          <p className="text-gray-600 dark:text-gray-300 text-sm mb-4">
            Download all your personal data in a machine-readable format (JSON). This includes your account information,
            instances, and usage data.
          </p>
          <button
            onClick={handleExportData}
            disabled={loading === 'export'}
            className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {loading === 'export' ? (
              <><Loader2 className="h-4 w-4 animate-spin" /> Exporting...</>
            ) : (
              <><Download className="h-4 w-4" /> Export My Data</>
            )}
          </button>
        </div>

        <CardSection>
          <h3 className="font-semibold text-gray-900 dark:text-white mb-4">Privacy Preferences</h3>
          <div className="space-y-4">
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={consentSettings.marketing}
                onChange={() => handleConsentUpdate('marketing')}
                disabled={loading === 'consent'}
                className="h-4 w-4 text-blue-600 rounded focus:ring-blue-500"
              />
              <div>
                <span className="text-gray-900 dark:text-white">Marketing Communications</span>
                <p className="text-gray-500 dark:text-gray-400 text-sm">
                  Receive updates about new features and MindRoom news
                </p>
              </div>
            </label>

            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={consentSettings.analytics}
                onChange={() => handleConsentUpdate('analytics')}
                disabled={loading === 'consent'}
                className="h-4 w-4 text-blue-600 rounded focus:ring-blue-500"
              />
              <div>
                <span className="text-gray-900 dark:text-white">Usage Analytics</span>
                <p className="text-gray-500 dark:text-gray-400 text-sm">
                  Help us improve by sharing anonymous usage data
                </p>
              </div>
            </label>
          </div>
        </CardSection>

        <CardSection>
          <h3 className="font-semibold text-gray-900 dark:text-white mb-4">Data Retention</h3>
          <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-4 text-sm space-y-2">
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-green-600 dark:text-green-400" />
              <span className="text-gray-700 dark:text-gray-300">
                <strong>Personal data:</strong> Deleted immediately when you close your account
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-green-600 dark:text-green-400" />
              <span className="text-gray-700 dark:text-gray-300">
                <strong>Payment info:</strong> We don't store payment details - Stripe handles this
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-green-600 dark:text-green-400" />
              <span className="text-gray-700 dark:text-gray-300">
                <strong>Invoices:</strong> Only invoice numbers kept (anonymized) for tax compliance
              </span>
            </div>
          </div>
        </CardSection>
      </Card>

      {/* Danger Zone */}
      {!isDeletionPending && (
        <Card variant="danger">
          <CardHeader>Danger Zone</CardHeader>

          <div className="mt-6">
            <h3 className="font-semibold text-red-900 dark:text-red-100 mb-4">Delete Account</h3>
            <p className="text-red-700 dark:text-red-200 text-sm mb-4">
              Once you delete your account, all your data will be permanently removed after a 7-day grace period.
              This action cannot be undone after the grace period expires.
            </p>

            {showDeleteConfirm ? (
              <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
                <p className="text-red-800 dark:text-red-200 font-semibold mb-3">
                  Are you absolutely sure?
                </p>
                <p className="text-red-700 dark:text-red-300 text-sm mb-4">
                  This will schedule your account for deletion. You'll have 7 days to change your mind.
                </p>
                <div className="flex gap-3">
                  <button
                    onClick={handleDeleteAccount}
                    disabled={loading === 'delete'}
                    className="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                  >
                    {loading === 'delete' ? (
                      <><Loader2 className="h-4 w-4 animate-spin" /> Deleting...</>
                    ) : (
                      'Yes, Delete My Account'
                    )}
                  </button>
                  <button
                    onClick={() => setShowDeleteConfirm(false)}
                    className="bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-900 dark:text-white px-4 py-2 rounded-lg transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <button
                onClick={handleDeleteAccount}
                className="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg transition-colors flex items-center gap-2"
              >
                <Trash2 className="h-4 w-4" /> Delete Account
              </button>
            )}
          </div>
        </Card>
      )}
    </div>
  )
}
