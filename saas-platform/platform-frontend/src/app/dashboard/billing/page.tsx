'use client'

import { useState } from 'react'
import { useSubscription } from '@/hooks/useSubscription'
import { createPortalSession } from '@/lib/api'
import { Loader2, CreditCard, TrendingUp, Check } from 'lucide-react'

export default function BillingPage() {
  const { subscription, loading } = useSubscription()
  const [redirecting, setRedirecting] = useState(false)

  const openStripePortal = async () => {
    setRedirecting(true)

    try {
      const { url } = await createPortalSession()
      window.location.href = url
    } catch (error) {
      console.error('Error opening Stripe portal:', error)
      setRedirecting(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 className="w-8 h-8 animate-spin text-orange-500" />
      </div>
    )
  }

  const getTierDisplay = (tier: string) => {
    const tiers: { [key: string]: { name: string; price: string; color: string } } = {
      free: { name: 'Free', price: '$0/month', color: 'gray' },
      starter: { name: 'Starter', price: '$10/month', color: 'blue' },
      professional: { name: 'Professional', price: '$8/user/month', color: 'purple' },
      enterprise: { name: 'Enterprise', price: 'Custom', color: 'yellow' },
    }
    return tiers[tier] || tiers.free
  }

  const tierInfo = getTierDisplay(subscription?.tier || 'free')

  const planFeatures = {
    free: [
      '1 AI Agent',
      '100 messages per day',
      '1GB storage',
      'Community support',
      'Basic integrations',
    ],
    starter: [
      '100 AI Agents',
      'Unlimited messages',
      '5GB storage',
      'Priority support',
      'All integrations',
      'Custom workflows',
      'Analytics dashboard',
    ],
    professional: [
      'Unlimited AI Agents',
      'Unlimited messages',
      '10GB storage per user',
      'Priority support',
      'Advanced analytics',
      'Custom integrations',
      'SLA guarantee',
      'Team training',
    ],
    enterprise: [
      'Unlimited everything',
      'Custom limits',
      'Dedicated infrastructure',
      'White-glove support',
      'Custom development',
      'On-premise option',
      'Compliance certifications',
      'Dedicated account manager',
    ],
  }

  const features = planFeatures[subscription?.tier || 'free'] || planFeatures.free

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold dark:text-white">Billing & Subscription</h1>

      {/* Current Plan */}
      <div className="bg-white dark:bg-gray-800 rounded-lg p-6 shadow-sm">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-xl font-bold mb-2 dark:text-white">Current Plan</h2>
            <div className="flex items-center gap-3 mb-4">
              <span className={`px-3 py-1 rounded-full text-sm font-medium bg-orange-100 text-orange-700`}>
                {tierInfo.name}
              </span>
              <span className="text-2xl font-bold">{tierInfo.price}</span>
              {subscription?.status === 'active' && (
                <span className="px-2 py-1 rounded-full text-xs font-medium bg-green-100 text-green-700">
                  Active
                </span>
              )}
            </div>
          </div>

          {subscription?.stripe_customer_id && (
            <button
              onClick={openStripePortal}
              disabled={redirecting}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {redirecting ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <CreditCard className="w-4 h-4" />
              )}
              Manage Subscription
            </button>
          )}
        </div>

        {/* Plan Details */}
        <div className="mt-6 pt-6 border-t">
          <h3 className="font-semibold mb-3">Plan Includes:</h3>
          <div className="grid md:grid-cols-2 gap-3">
            {features.map((feature, index) => (
              <div key={index} className="flex items-center gap-2">
                <Check className="w-4 h-4 text-green-500" />
                <span className="text-sm">{feature}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Usage Limits */}
        <div className="mt-6 pt-6 border-t">
          <h3 className="font-semibold mb-3">Usage Limits:</h3>
          <div className="grid md:grid-cols-3 gap-4">
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400 dark:text-gray-500 dark:text-gray-400" />
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">AI Agents</p>
                <p className="font-semibold">{subscription?.max_agents || 1}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400 dark:text-gray-500 dark:text-gray-400" />
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">Messages/Day</p>
                <p className="font-semibold">{subscription?.max_messages_per_day.toLocaleString() || 100}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400 dark:text-gray-500 dark:text-gray-400" />
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">Storage</p>
                <p className="font-semibold">{subscription?.max_storage_gb || 1}GB</p>
              </div>
            </div>
          </div>
        </div>

        {/* Billing Period */}
        {subscription?.current_period_end && (
          <div className="mt-6 pt-6 border-t">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              Next billing date: <span className="font-medium">{new Date(subscription.current_period_end).toLocaleDateString()}</span>
            </p>
          </div>
        )}
      </div>

      {/* Payment Method */}
      <div className="bg-white dark:bg-gray-800 rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4 dark:text-white">Payment Method</h2>
        {subscription?.stripe_customer_id ? (
          <>
            <p className="text-gray-600 dark:text-gray-400 mb-4">
              Manage your payment methods and billing information through the Stripe customer portal.
            </p>
            <button
              onClick={openStripePortal}
              className="text-blue-600 hover:text-blue-700 font-medium"
            >
              Update Payment Method →
            </button>
          </>
        ) : (
          <>
            <p className="text-gray-600 mb-4">
              No payment method on file. Upgrade your plan to add a payment method.
            </p>
            <button
              onClick={() => window.location.href = '/dashboard/billing/upgrade'}
              className="text-orange-600 hover:text-orange-700 font-medium"
            >
              Upgrade Plan →
            </button>
          </>
        )}
      </div>

      {/* Upgrade CTA for Free Users */}
      {subscription?.tier === 'free' && (
        <div className="bg-gradient-to-r from-orange-50 to-yellow-50 dark:from-orange-900/20 dark:to-yellow-900/20 rounded-lg p-6 border border-orange-200 dark:border-orange-800">
          <h2 className="text-xl font-bold mb-2 dark:text-white">Ready to scale?</h2>
          <p className="text-gray-600 dark:text-gray-400 mb-4">
            Unlock more agents, higher limits, and premium features with our paid plans.
          </p>
          <button
            onClick={() => window.location.href = '/dashboard/billing/upgrade'}
            className="px-4 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors"
          >
            View Upgrade Options
          </button>
        </div>
      )}
    </div>
  )
}
