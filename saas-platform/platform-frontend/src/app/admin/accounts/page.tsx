'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { Card, CardContent, CardHeader } from '@/components/ui/Card'
import { Button } from '@/components/ui/button'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface Account {
  id: string
  email: string
  full_name: string | null
  company_name: string | null
  status: string
  is_admin: boolean
  created_at: string
}

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loading, setLoading] = useState(true)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [editStatuses, setEditStatuses] = useState<Record<string, string>>({})
  const [deletingId, setDeletingId] = useState<string | null>(null)

  useEffect(() => {
    const fetchAccounts = async () => {
      try {
        const response = await apiCall('/admin/accounts')
        if (response.ok) {
          const data = await response.json()
          // Generic admin list endpoint returns { data, total }
          setAccounts(data.data || [])
        } else {
          logger.error('Failed to fetch accounts:', response.statusText)
        }
      } catch (error) {
        logger.error('Error fetching accounts:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchAccounts()
  }, [])

  const handleDelete = async (accountId: string, accountEmail: string) => {
    const confirmMessage = `Are you sure you want to permanently delete the account for ${accountEmail}?\n\n` +
      `This will:\n` +
      `• Deprovision all MindRoom instances (Matrix server, backend, frontend)\n` +
      `• Cancel any active Stripe subscriptions\n` +
      `• Delete the account and all associated data\n\n` +
      `This action cannot be undone.`

    if (!confirm(confirmMessage)) {
      return
    }

    setDeletingId(accountId)
    try {
      const response = await apiCall(`/admin/accounts/${accountId}/complete`, { method: 'DELETE' })
      if (response.ok) {
        // Remove the account from the list
        setAccounts(prev => prev.filter(a => a.id !== accountId))
        alert(`Successfully deleted account ${accountEmail} and all associated resources.`)
      } else {
        const error = await response.text()
        logger.error('Failed to delete account:', error)
        alert(`Failed to delete account: ${error}`)
      }
    } catch (error) {
      logger.error('Error deleting account:', error)
      alert('An error occurred while deleting the account')
    } finally {
      setDeletingId(null)
    }
  }

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
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Accounts</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-2">Manage user accounts and permissions</p>
        </div>
        <Button>Export</Button>
      </div>

      <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
        <CardHeader className="text-gray-900 dark:text-gray-100">All Accounts</CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Email</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Full Name</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Company</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Status</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Admin</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Created</th>
                  <th className="text-left py-3 px-4 text-gray-700 dark:text-gray-300">Actions</th>
                </tr>
              </thead>
              <tbody>
                {accounts?.map((account) => (
                  <tr key={account.id} className="border-b border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">
                    <td className="py-3 px-4">
                      <div className="font-medium text-gray-900 dark:text-gray-100">{account.email}</div>
                    </td>
                    <td className="py-3 px-4 text-gray-700 dark:text-gray-300">{account.full_name || '-'}</td>
                    <td className="py-3 px-4 text-gray-700 dark:text-gray-300">{account.company_name || '-'}</td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <select
                          className="text-sm border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                          value={editStatuses[account.id] ?? account.status}
                          onChange={(e) => setEditStatuses((s) => ({ ...s, [account.id]: e.target.value }))}
                        >
                          <option value="active">active</option>
                          <option value="suspended">suspended</option>
                          <option value="deleted">deleted</option>
                          <option value="pending_verification">pending_verification</option>
                        </select>
                        <button
                          className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 text-sm disabled:text-gray-400"
                          disabled={updatingId === account.id}
                          onClick={async () => {
                            const next = editStatuses[account.id] ?? account.status
                            setUpdatingId(account.id)
                            try {
                              const res = await apiCall(`/admin/accounts/${account.id}/status?status=${encodeURIComponent(next)}`, { method: 'PUT' })
                              if (!res.ok) throw new Error('Failed to update status')
                              setAccounts((prev) => prev.map(a => a.id === account.id ? { ...a, status: next } : a))
                            } catch (err) {
                              logger.error('Update status failed', err)
                            } finally {
                              setUpdatingId(null)
                            }
                          }}
                        >
                          {updatingId === account.id ? 'Saving...' : 'Save'}
                        </button>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      {account.is_admin ? (
                        <span className="text-green-600 dark:text-green-400">✓</span>
                      ) : (
                        <span className="text-gray-400 dark:text-gray-500">-</span>
                      )}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                      {new Date(account.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300"
                          onClick={() => window.location.href = `/admin/accounts/${account.id}`}
                        >
                          View
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => handleDelete(account.id, account.email)}
                          disabled={deletingId === account.id}
                        >
                          {deletingId === account.id ? 'Deleting...' : 'Delete'}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {(!accounts || accounts.length === 0) && (
              <div className="text-center py-8 text-gray-500">
                No accounts found
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
