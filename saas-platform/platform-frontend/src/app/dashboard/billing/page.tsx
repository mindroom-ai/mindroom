'use client'

import { useState, useEffect } from 'react'
import { useSubscription } from '@/hooks/useSubscription'
import { createPortalSession } from '@/lib/api'
import { PRICING_PLANS, formatLimit, type PlanId } from '@/lib/pricing-config'
import { Loader2, CreditCard, TrendingUp, Check } from 'lucide-react'

export default function BillingPage() {
  const { subscription, loading, refresh } = useSubscription()
  const [redirecting, setRedirecting] = useState(false)

  // Auto-refresh when returning from Stripe portal or checkout
  useEffect(() => {
    // Check if we're returning from Stripe
    const urlParams = new URLSearchParams(window.location.search)
    if (urlParams.has('success') || urlParams.has('return')) {
      // Clear the URL params
      window.history.replaceState({}, document.title, window.location.pathname)
      // Refresh subscription data
      if (refresh) {
        refresh()
      }
    }

    // Also refresh on page focus (when user returns from another tab)
    const handleFocus = () => {
      if (document.hasFocus() && refresh) {
        refresh()
      }
    }

    window.addEventListener('focus', handleFocus)
    return () => window.removeEventListener('focus', handleFocus)
  }, [refresh])

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

  const currentTier = (subscription?.tier || 'free') as PlanId
  const currentPlan = PRICING_PLANS[currentTier]
  const features = currentPlan.features
  const tierInfo = {
    name: currentPlan.name,
    price: currentPlan.price + currentPlan.period,
  }

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
              {subscription?.status === 'trialing' && (
                <span className="px-2 py-1 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
                  Trial
                </span>
              )}
              {subscription?.status === 'past_due' && (
                <span className="px-2 py-1 rounded-full text-xs font-medium bg-red-100 text-red-700">
                  Past Due
                </span>
              )}
              {subscription?.status === 'cancelled' && (
                <span className="px-2 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-700">
                  Cancelled
                </span>
              )}
              {subscription?.cancelled_at && subscription?.status !== 'cancelled' && (
                <span className="px-2 py-1 rounded-full text-xs font-medium bg-yellow-100 text-yellow-700">
                  Cancelling
                </span>
              )}
            </div>
          </div>

          {subscription?.stripe_subscription_id && (
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
                <p className="font-semibold">{formatLimit(currentPlan.limits.maxAgents)}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400 dark:text-gray-500 dark:text-gray-400" />
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">Messages/Day</p>
                <p className="font-semibold">{formatLimit(currentPlan.limits.maxMessagesPerDay)}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400 dark:text-gray-500 dark:text-gray-400" />
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">Storage</p>
                <p className="font-semibold">
                  {currentPlan.limits.storageGb === 'unlimited' ? 'Unlimited' : `${currentPlan.limits.storageGb}GB`}
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Billing Period */}
        {(subscription?.current_period_end || subscription?.trial_ends_at || subscription?.cancelled_at) && (
          <div className="mt-6 pt-6 border-t">
            {subscription?.cancelled_at && subscription?.status !== 'cancelled' ? (
              <div className="space-y-2">
                <p className="text-sm text-yellow-600 dark:text-yellow-400 font-medium">
                  ⚠️ Subscription will end on:{' '}
                  <span className="font-bold">
                    {subscription?.trial_ends_at
                      ? new Date(subscription.trial_ends_at).toLocaleDateString()
                      : subscription?.current_period_end
                      ? new Date(subscription.current_period_end).toLocaleDateString()
                      : 'end of billing period'}
                  </span>
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  You can reactivate anytime before this date through the Manage Subscription portal.
                </p>
              </div>
            ) : subscription?.status === 'trialing' && subscription?.trial_ends_at ? (
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Trial ends: <span className="font-medium">{new Date(subscription.trial_ends_at).toLocaleDateString()}</span>
                {' '}({Math.ceil((new Date(subscription.trial_ends_at).getTime() - Date.now()) / (1000 * 60 * 60 * 24))} days remaining)
              </p>
            ) : subscription?.current_period_end ? (
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Next billing date: <span className="font-medium">{new Date(subscription.current_period_end).toLocaleDateString()}</span>
              </p>
            ) : null}
          </div>
        )}
      </div>

      {/* Payment Method */}
      <div className="bg-white dark:bg-gray-800 rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4 dark:text-white">Payment Method</h2>
        {subscription?.stripe_subscription_id ? (
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

      {/* Available Plans - Show for all users */}
      <div className="bg-white dark:bg-gray-800 rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4 dark:text-white">Available Plans</h2>
        <div className="grid md:grid-cols-3 gap-4">
          {Object.values(PRICING_PLANS)
            .filter(plan => plan.id !== 'free' && plan.id !== 'enterprise')
            .map(plan => {
              const isCurrentPlan = plan.id === currentTier
              const isDowngrade = ['starter', 'professional'].indexOf(plan.id) < ['starter', 'professional'].indexOf(currentTier)

              return (
                <div
                  key={plan.id}
                  className={`border rounded-lg p-4 ${
                    isCurrentPlan
                      ? 'border-orange-500 bg-orange-50 dark:bg-orange-900/20'
                      : isDowngrade
                      ? 'border-gray-200 dark:border-gray-700 opacity-50'
                      : 'border-gray-200 dark:border-gray-700 hover:border-orange-300 dark:hover:border-orange-600'
                  }`}
                >
                  <div className="flex justify-between items-start mb-2">
                    <h3 className="font-semibold text-lg">{plan.name}</h3>
                    {isCurrentPlan && (
                      <span className="text-xs px-2 py-1 bg-orange-500 text-white rounded-full">Current</span>
                    )}
                  </div>
                  <p className="text-2xl font-bold mb-2">
                    {plan.price}
                    <span className="text-sm text-gray-500 dark:text-gray-400">{plan.period}</span>
                  </p>
                  <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">{plan.description}</p>
                  {!isCurrentPlan && !isDowngrade && (
                    <button
                      onClick={() => window.location.href = '/dashboard/billing/upgrade'}
                      className="w-full px-3 py-2 bg-orange-500 text-white text-sm rounded-lg hover:bg-orange-600 transition-colors"
                    >
                      Upgrade to {plan.name}
                    </button>
                  )}
                  {isDowngrade && (
                    <p className="text-xs text-gray-500 dark:text-gray-400 text-center">
                      Contact support to downgrade
                    </p>
                  )}
                </div>
              )
            })}
        </div>
        <div className="mt-4 text-center">
          <button
            onClick={() => window.location.href = '/dashboard/billing/upgrade'}
            className="text-sm text-orange-600 hover:text-orange-700 font-medium"
          >
            View all plans and billing options →
          </button>
        </div>
      </div>
    </div>
  )
}
