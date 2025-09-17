'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/button'
import { ArrowLeft, User, Mail, Building, Calendar, Shield } from 'lucide-react'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

interface AccountDetails {
  id: string
  email: string
  full_name: string | null
  company_name: string | null
  status: string
  is_admin: boolean
  created_at: string
  updated_at: string
  subscription?: {
    id: string
    tier: string
    status: string
    price: number
    billing_period: string
  }
  instances?: Array<{
    id: string
    instance_id: string
    status: string
    created_at: string
  }>
}

export default function AccountDetailsPage() {
  const params = useParams()
  const router = useRouter()
  const [account, setAccount] = useState<AccountDetails | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchAccount = async () => {
      try {
        const response = await apiCall(`/admin/accounts/${params.id}`)
        if (response.ok) {
          const data = await response.json()
          setAccount(data)
        } else {
          logger.error('Failed to fetch account:', response.statusText)
        }
      } catch (error) {
        logger.error('Error fetching account:', error)
      } finally {
        setLoading(false)
      }
    }

    if (params.id) {
      fetchAccount()
    }
  }, [params.id])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-lg">Loading...</div>
      </div>
    )
  }

  if (!account) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-lg">Account not found</div>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-8">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => router.push('/admin/accounts')}
          className="mb-4"
        >
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Accounts
        </Button>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Account Details</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-2">View and manage account information</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Basic Information */}
        <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
          <CardHeader>
            <CardTitle className="flex items-center text-gray-900 dark:text-gray-100">
              <User className="w-5 h-5 mr-2" />
              Basic Information
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">Email</label>
              <div className="flex items-center mt-1">
                <Mail className="w-4 h-4 mr-2 text-gray-400" />
                <span className="text-gray-900 dark:text-gray-100">{account.email}</span>
              </div>
            </div>
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">Full Name</label>
              <div className="flex items-center mt-1">
                <User className="w-4 h-4 mr-2 text-gray-400" />
                <span className="text-gray-900 dark:text-gray-100">{account.full_name || 'Not provided'}</span>
              </div>
            </div>
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">Company</label>
              <div className="flex items-center mt-1">
                <Building className="w-4 h-4 mr-2 text-gray-400" />
                <span className="text-gray-900 dark:text-gray-100">{account.company_name || 'Not provided'}</span>
              </div>
            </div>
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">Created</label>
              <div className="flex items-center mt-1">
                <Calendar className="w-4 h-4 mr-2 text-gray-400" />
                <span className="text-gray-900 dark:text-gray-100">
                  {new Date(account.created_at).toLocaleDateString()}
                </span>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Account Status */}
        <Card className="bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800">
          <CardHeader>
            <CardTitle className="flex items-center text-gray-900 dark:text-gray-100">
              <Shield className="w-5 h-5 mr-2" />
              Account Status
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">Status</label>
              <div className="mt-1">
                <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                  account.status === 'active' ? 'bg-green-100 dark:bg-green-900/20 text-green-800 dark:text-green-400' :
                  account.status === 'suspended' ? 'bg-yellow-100 dark:bg-yellow-900/20 text-yellow-800 dark:text-yellow-400' :
                  'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-400'
                }`}>
                  {account.status}
                </span>
              </div>
            </div>
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">Admin Access</label>
              <div className="mt-1">
                {account.is_admin ? (
                  <span className="text-green-600 dark:text-green-400">✓ Admin</span>
                ) : (
                  <span className="text-gray-500 dark:text-gray-400">Regular User</span>
                )}
              </div>
            </div>
            {account.subscription && (
              <div>
                <label className="text-sm font-medium text-gray-500 dark:text-gray-400">Subscription</label>
                <div className="mt-1">
                  <span className="text-gray-900 dark:text-gray-100">
                    {account.subscription.tier} - ${(account.subscription.price / 100).toFixed(2)}/{account.subscription.billing_period}
                  </span>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Actions */}
      <div className="mt-6 flex gap-3">
        <Button variant="outline">
          Suspend Account
        </Button>
        <Button variant="outline">
          Reset Password
        </Button>
        <Button variant="outline" className="text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">
          Delete Account
        </Button>
      </div>
    </div>
  )
}
