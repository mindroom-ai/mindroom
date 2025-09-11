'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/button'
import { apiCall } from '@/lib/api'

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

  useEffect(() => {
    const fetchAccounts = async () => {
      try {
        const response = await apiCall('/admin/accounts')
        if (response.ok) {
          const data = await response.json()
          // Generic admin list endpoint returns { data, total }
          setAccounts(data.data || [])
        } else {
          console.error('Failed to fetch accounts:', response.statusText)
        }
      } catch (error) {
        console.error('Error fetching accounts:', error)
      } finally {
        setLoading(false)
      }
    }

    fetchAccounts()
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
          <h1 className="text-3xl font-bold text-gray-900">Accounts</h1>
          <p className="text-gray-600 mt-2">Manage user accounts and permissions</p>
        </div>
        <Button>Export</Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>All Accounts</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-3 px-4">Email</th>
                  <th className="text-left py-3 px-4">Full Name</th>
                  <th className="text-left py-3 px-4">Company</th>
                  <th className="text-left py-3 px-4">Status</th>
                  <th className="text-left py-3 px-4">Admin</th>
                  <th className="text-left py-3 px-4">Created</th>
                  <th className="text-left py-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {accounts?.map((account) => (
                  <tr key={account.id} className="border-b hover:bg-gray-50">
                    <td className="py-3 px-4">
                      <div className="font-medium">{account.email}</div>
                    </td>
                    <td className="py-3 px-4">{account.full_name || '-'}</td>
                    <td className="py-3 px-4">{account.company_name || '-'}</td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <select
                          className="text-sm border border-gray-300 rounded px-2 py-1 bg-white"
                          value={editStatuses[account.id] ?? account.status}
                          onChange={(e) => setEditStatuses((s) => ({ ...s, [account.id]: e.target.value }))}
                        >
                          <option value="active">active</option>
                          <option value="suspended">suspended</option>
                          <option value="deleted">deleted</option>
                          <option value="pending_verification">pending_verification</option>
                        </select>
                        <button
                          className="text-blue-600 hover:text-blue-900 text-sm disabled:text-gray-400"
                          disabled={updatingId === account.id}
                          onClick={async () => {
                            const next = editStatuses[account.id] ?? account.status
                            setUpdatingId(account.id)
                            try {
                              const res = await apiCall(`/admin/accounts/${account.id}/status?status=${encodeURIComponent(next)}`, { method: 'PUT' })
                              if (!res.ok) throw new Error('Failed to update status')
                              setAccounts((prev) => prev.map(a => a.id === account.id ? { ...a, status: next } : a))
                            } catch (err) {
                              console.error('Update status failed', err)
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
                        <span className="text-green-600">✓</span>
                      ) : (
                        <span className="text-gray-400">-</span>
                      )}
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500">
                      {new Date(account.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4">
                      <Link href={`/admin/accounts/${account.id}`} className="text-blue-600 hover:text-blue-900 text-sm">
                        View
                      </Link>
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
