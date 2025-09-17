'use client'

import { useState, useEffect } from 'react'
import { useSubscription } from '@/hooks/useSubscription'
import { createPortalSession, getPricingConfig } from '@/lib/api'
import { logger } from '@/lib/logger'
import { PLAN_GRADIENTS, type PlanId } from '@/lib/pricing-config'
import { DashboardLoader } from '@/components/dashboard/DashboardLoader'
import { Loader2, CreditCard, TrendingUp, Check, RefreshCw } from 'lucide-react'

interface PricingPlan {
  name: string
  price_monthly: string
  price_yearly: string
  price_model?: string
  description: string
  features: string[]
  recommended?: boolean
  limits?: {
    max_agents: number | string
    max_messages_per_day: number | string
    storage_gb: number | string
  }
}

interface PricingConfig {
  plans: Record<string, PricingPlan>
  discounts?: {
    annual_percentage: number
  }
}

function formatLimit(value: number | string | undefined): string {
  if (!value) return 'N/A'
  if (value === 'unlimited' || value === -1) return 'Unlimited'
  if (typeof value === 'number') {
    if (value >= 1000000) return `${value / 1000000}M`
    if (value >= 1000) return `${value / 1000}K`
    return value.toString()
  }
  return value
}

export default function BillingPage() {
  const { subscription, loading, refresh } = useSubscription()
  const [redirecting, setRedirecting] = useState(false)
  const [pricingConfig, setPricingConfig] = useState<PricingConfig | null>(null)
  const [pricingLoading, setPricingLoading] = useState(true)

  // Fetch pricing configuration
  useEffect(() => {
    getPricingConfig()
      .then(setPricingConfig)
      .catch(logger.error)
      .finally(() => setPricingLoading(false))
  }, [])

  // Auto-refresh when returning from Stripe portal or checkout
  useEffect(() => {
    // Check if we're returning from Stripe
    const urlParams = new URLSearchParams(window.location.search)
    if (urlParams.has('success') || urlParams.has('return')) {
      // Clear the URL params
      window.history.replaceState({}, document.title, window.location.pathname)
      // Force refresh subscription data (clears cache)
      if (refresh) {
        refresh(true)
      }
    }
  }, [refresh])

  const openStripePortal = async () => {
    setRedirecting(true)

    try {
      const { url } = await createPortalSession()
      window.location.href = url
    } catch (error) {
      logger.error('Error opening Stripe portal:', error)
      setRedirecting(false)
    }
  }

  if (loading || pricingLoading) {
    return <DashboardLoader message="Loading billing information..." />
  }

  if (!pricingConfig) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-red-500">Failed to load pricing configuration</div>
      </div>
    )
  }

  const currentTier = (subscription?.tier || 'free') as PlanId
  const currentPlan = pricingConfig.plans[currentTier]
  const features = currentPlan?.features || []
  const tierInfo = {
    name: currentPlan?.name || 'Free',
    price: currentPlan ?
      `${currentPlan.price_monthly}${currentPlan.price_model === 'per_user' ? '/user/month' : '/month'}` :
      '$0/month',
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold dark:text-white">Billing & Subscription</h1>
        <button
          onClick={() => refresh(true)}
          className="flex items-center gap-2 px-3 py-1.5 text-sm text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 transition-colors"
          title="Refresh subscription data"
        >
          <RefreshCw className="w-4 h-4" />
          Refresh
        </button>
      </div>

      {/* Cancellation Warning Banner */}
      {subscription?.cancelled_at && subscription?.status !== 'cancelled' && (
        <div className="bg-yellow-50 dark:bg-yellow-900/20 border-2 border-yellow-400 dark:border-yellow-600 rounded-lg p-4 mb-6">
          <div className="flex items-start">
            <div className="flex-shrink-0">
              <svg className="h-6 w-6 text-yellow-600" fill="none" viewBox="0 0 24 24" strokeWidth="2" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
              </svg>
            </div>
            <div className="ml-3 flex-1">
              <h3 className="text-sm font-bold text-yellow-800 dark:text-yellow-200">
                SUBSCRIPTION ENDING SOON
              </h3>
              <div className="mt-2 text-sm text-yellow-700 dark:text-yellow-300">
                <p>Your {tierInfo.name} subscription will end on <strong>{subscription?.trial_ends_at
                  ? new Date(subscription.trial_ends_at).toLocaleDateString()
                  : subscription?.current_period_end
                  ? new Date(subscription.current_period_end).toLocaleDateString()
                  : 'the end of your billing period'}</strong></p>
                <p className="mt-1">After this date, your account will revert to the Free plan.</p>
              </div>
              <div className="mt-3">
                <button
                  onClick={openStripePortal}
                  className="text-sm font-medium text-yellow-800 dark:text-yellow-200 hover:text-yellow-600 dark:hover:text-yellow-100"
                >
                  Reactivate subscription →
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Current Plan */}
      <div className="bg-white dark:bg-gray-800 rounded-lg p-6 shadow-sm">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-xl font-bold mb-2 dark:text-white">Current Plan</h2>
            <div className="flex items-center gap-3 mb-2">
              <span className={`px-3 py-1 rounded-full text-sm font-medium ${
                subscription?.cancelled_at && subscription?.status !== 'cancelled'
                  ? 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300'
                  : 'bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300'
              }`}>
                {tierInfo.name}
              </span>
              <span className="text-2xl font-bold">{tierInfo.price}</span>
              {subscription?.status === 'active' && !subscription?.cancelled_at && (
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
            </div>

            {/* Trial/Billing Period Information */}
            {subscription && (
              <div className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                {subscription.status === 'trialing' && subscription.trial_ends_at && (
                  <div>
                    <p className="text-base">
                      {subscription.cancelled_at ? (
                        <>
                          <span className="text-yellow-600 dark:text-yellow-400 font-semibold">Cancels {new Date(subscription.trial_ends_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</span>
                          <br />
                          <span className="text-sm">After your free trial ends on {new Date(subscription.trial_ends_at).toLocaleDateString('en-US', {
                            month: 'long',
                            day: 'numeric',
                            year: 'numeric'
                          })}, this subscription will no longer be available.</span>
                        </>
                      ) : (
                        <>
                          Free trial until: <strong className="text-gray-900 dark:text-gray-100">
                            {new Date(subscription.trial_ends_at).toLocaleDateString('en-US', {
                              month: 'long',
                              day: 'numeric',
                              year: 'numeric'
                            })}
                          </strong>
                          <span className="text-gray-500 dark:text-gray-400 ml-2">
                            ({Math.ceil((new Date(subscription.trial_ends_at).getTime() - Date.now()) / (1000 * 60 * 60 * 24))} days remaining)
                          </span>
                        </>
                      )}
                    </p>
                  </div>
                )}
                {subscription.status === 'active' && subscription.current_period_end && !subscription.cancelled_at && (
                  <p>
                    Next billing date: <strong className="text-gray-900 dark:text-gray-100">
                      {new Date(subscription.current_period_end).toLocaleDateString('en-US', {
                        month: 'long',
                        day: 'numeric',
                        year: 'numeric'
                      })}
                    </strong>
                  </p>
                )}
                {subscription.cancelled_at && subscription.status === 'active' && subscription.current_period_end && (
                  <p>
                    <span className="text-yellow-600 dark:text-yellow-400 font-semibold">Cancels {new Date(subscription.current_period_end).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</span>
                    <br />
                    <span className="text-sm">Subscription ends on {new Date(subscription.current_period_end).toLocaleDateString('en-US', {
                      month: 'long',
                      day: 'numeric',
                      year: 'numeric'
                    })}</span>
                  </p>
                )}
              </div>
            )}
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
                <p className="font-semibold">{formatLimit(currentPlan?.limits?.max_agents)}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400 dark:text-gray-500 dark:text-gray-400" />
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">Messages/Day</p>
                <p className="font-semibold">{formatLimit(currentPlan?.limits?.max_messages_per_day)}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400 dark:text-gray-500 dark:text-gray-400" />
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">Storage</p>
                <p className="font-semibold">
                  {formatLimit(currentPlan?.limits?.storage_gb) === 'Unlimited' ? 'Unlimited' : `${formatLimit(currentPlan?.limits?.storage_gb)}GB`}
                </p>
              </div>
            </div>
          </div>
        </div>

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
          {Object.entries(pricingConfig.plans)
            .filter(([key]) => key !== 'free' && key !== 'enterprise')
            .map(([key, plan]) => {
              const isCurrentPlan = key === currentTier
              const isDowngrade = ['starter', 'professional'].indexOf(key) < ['starter', 'professional'].indexOf(currentTier)

              return (
                <div
                  key={key}
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
                    {plan.price_monthly}
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      {plan.price_model === 'per_user' ? '/user/month' : '/month'}
                    </span>
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
