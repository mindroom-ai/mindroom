'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { Check, ArrowLeft, Sparkles } from 'lucide-react'
import { useSubscription } from '@/hooks/useSubscription'
import { createCheckoutSession } from '@/lib/api'
import { PRICING_PLANS, type PlanId } from '@/lib/pricing-config'

// Filter out free plan and map to upgrade options
const plans = Object.values(PRICING_PLANS)
  .filter(plan => plan.id !== 'free')
  .map(plan => ({
    id: plan.id,
    name: plan.name,
    price: plan.price,
    description: plan.description,
    features: plan.features,
    recommended: plan.recommended,
  }))

// Pricing with yearly discount (20% off)
const PRICING_DETAILS = {
  starter: {
    monthly: { price: '$10', period: '/month', total: '$10' },
    yearly: { price: '$8', period: '/month', total: '$96/year', savings: 'Save $24' }
  },
  professional: {
    monthly: { price: '$8', period: '/user/month', total: '$8/user' },
    yearly: { price: '$6.40', period: '/user/month', total: '$76.80/user/year', savings: 'Save 20%' }
  },
  enterprise: {
    monthly: { price: 'Custom', period: '', total: 'Contact Sales' },
    yearly: { price: 'Custom', period: '', total: 'Contact Sales', savings: '' }
  }
} as const

export default function UpgradePage() {
  const router = useRouter()
  const { subscription, loading } = useSubscription()
  const [selectedPlan, setSelectedPlan] = useState<string | null>(null)
  const [billingCycle, setBillingCycle] = useState<'monthly' | 'yearly'>('monthly')
  const [isProcessing, setIsProcessing] = useState(false)
  const [userCount, setUserCount] = useState(1)

  useEffect(() => {
    // Pre-select the recommended plan if user is on free tier
    if (!loading && subscription?.tier === 'free') {
      setSelectedPlan('starter')
    }
  }, [subscription, loading])

  const handleUpgrade = async () => {
    if (!selectedPlan) return

    const plan = plans.find(p => p.id === selectedPlan)
    if (!plan) return

    if (plan.id === 'enterprise') {
      window.location.href = 'mailto:sales@mindroom.chat?subject=Enterprise Plan Inquiry'
      return
    }

    setIsProcessing(true)

    try {
      // Pass quantity for professional plan
      const quantity = plan.id === 'professional' ? userCount : 1
      const { url } = await createCheckoutSession(plan.id, billingCycle, quantity)
      window.location.href = url
    } catch (error) {
      console.error('Error creating checkout session:', error)
      alert('An error occurred. Please try again.')
      setIsProcessing(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-orange-500"></div>
      </div>
    )
  }

  const currentTier = subscription?.tier || 'free'

  return (
    <div className="max-w-6xl mx-auto p-6">
      {/* Header */}
      <div className="mb-8">
        <button
          onClick={() => router.push('/dashboard/billing')}
          className="flex items-center text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 mb-4"
        >
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Billing
        </button>
        <h1 className="text-3xl font-bold dark:text-white">Upgrade Your Plan</h1>
        {process.env.NODE_ENV === 'development' || process.env.NEXT_PUBLIC_STRIPE_MODE === 'test' ? (
          <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-400 dark:border-yellow-600 rounded-lg p-3 mt-4">
            <p className="text-sm text-yellow-800 dark:text-yellow-200 font-semibold">Test Mode Active</p>
            <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
              Use test card: <code className="bg-yellow-100 dark:bg-yellow-800 px-1 rounded">4242 4242 4242 4242</code> with any future date and CVC.
            </p>
          </div>
        ) : null}
        <p className="text-gray-600 dark:text-gray-400 mt-2">
          Choose a plan that fits your needs. You can change or cancel anytime.
        </p>
        {currentTier !== 'free' && (
          <p className="text-sm text-orange-600 dark:text-orange-400 mt-2">
            Currently on {currentTier} plan. Upgrading will prorate your billing.
          </p>
        )}
      </div>

      {/* Billing Cycle Toggle */}
      <div className="flex justify-center mb-8">
        <div className="bg-gray-100 dark:bg-gray-800 p-1 rounded-lg inline-flex">
          <button
            onClick={() => setBillingCycle('monthly')}
            className={`px-6 py-2 rounded-md text-sm font-medium transition-all ${
              billingCycle === 'monthly'
                ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            Monthly
          </button>
          <button
            onClick={() => setBillingCycle('yearly')}
            className={`px-6 py-2 rounded-md text-sm font-medium transition-all flex items-center ${
              billingCycle === 'yearly'
                ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            Yearly
            <span className="ml-2 px-2 py-0.5 bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300 text-xs rounded-full">
              Save 20%
            </span>
          </button>
        </div>
      </div>

      {/* Plans Grid */}
      <div className="grid md:grid-cols-3 gap-6 mb-8">
        {plans.map((plan) => {
          const isCurrentPlan = plan.id === currentTier
          const isDowngrade = plans.findIndex(p => p.id === plan.id) < plans.findIndex(p => p.id === currentTier)
          const pricing = PRICING_DETAILS[plan.id as keyof typeof PRICING_DETAILS][billingCycle]

          return (
            <div
              key={plan.id}
              onClick={() => !isCurrentPlan && !isDowngrade && setSelectedPlan(plan.id)}
              className={`
                relative rounded-lg border-2 p-6 cursor-pointer transition-all
                ${selectedPlan === plan.id ? 'border-orange-500 bg-orange-50 dark:bg-orange-900/10' : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'}
                ${isCurrentPlan ? 'opacity-50 cursor-not-allowed' : ''}
                ${isDowngrade ? 'opacity-50 cursor-not-allowed' : ''}
              `}
            >
              {plan.recommended && !isCurrentPlan && (
                <div className="absolute -top-3 left-1/2 transform -translate-x-1/2 z-10">
                  <span className="bg-orange-500 text-white px-3 py-1 rounded-full text-xs font-semibold flex items-center">
                    <Sparkles className="w-3 h-3 mr-1" />
                    Recommended
                  </span>
                </div>
              )}

              {isCurrentPlan && (
                <div className="absolute -top-3 left-1/2 transform -translate-x-1/2 z-10">
                  <span className="bg-gray-500 text-white px-3 py-1 rounded-full text-xs font-semibold">
                    Current Plan
                  </span>
                </div>
              )}

              <div className="mb-4">
                <h3 className="text-xl font-bold dark:text-white">{plan.name}</h3>
                <p className="text-gray-600 dark:text-gray-400 text-sm mt-1">{plan.description}</p>
              </div>

              <div className="mb-6">
                <div className="flex items-baseline">
                  <span className="text-3xl font-bold dark:text-white">{pricing.price}</span>
                  {pricing.period && <span className="text-gray-600 dark:text-gray-400 ml-1">{pricing.period}</span>}
                </div>
                {billingCycle === 'yearly' && pricing.savings && (
                  <div className="mt-2">
                    <span className="text-sm text-green-600 dark:text-green-400 font-medium">{pricing.savings}</span>
                    <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                      Billed as {pricing.total}
                    </div>
                  </div>
                )}
                {plan.id === 'professional' && billingCycle === 'monthly' && (
                  <div className="mt-3 space-y-2">
                    <label className="text-sm text-gray-600 dark:text-gray-400">Number of users:</label>
                    <input
                      type="number"
                      min="1"
                      max="100"
                      value={userCount}
                      onChange={(e) => setUserCount(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))}
                      onClick={(e) => e.stopPropagation()}
                      className="w-full px-3 py-1 border border-gray-300 dark:border-gray-600 rounded-md text-sm dark:bg-gray-800 dark:text-white"
                      disabled={selectedPlan !== plan.id}
                    />
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      Total: ${(8 * userCount).toFixed(2)}/month
                    </div>
                  </div>
                )}
                {plan.id === 'professional' && billingCycle === 'yearly' && (
                  <div className="mt-3 space-y-2">
                    <label className="text-sm text-gray-600 dark:text-gray-400">Number of users:</label>
                    <input
                      type="number"
                      min="1"
                      max="100"
                      value={userCount}
                      onChange={(e) => setUserCount(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))}
                      onClick={(e) => e.stopPropagation()}
                      className="w-full px-3 py-1 border border-gray-300 dark:border-gray-600 rounded-md text-sm dark:bg-gray-800 dark:text-white"
                      disabled={selectedPlan !== plan.id}
                    />
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      Total: ${(76.80 * userCount).toFixed(2)}/year
                    </div>
                  </div>
                )}
              </div>

              <ul className="space-y-3">
                {plan.features.map((feature, index) => (
                  <li key={index} className="flex items-start">
                    <Check className="w-5 h-5 text-green-500 mr-2 flex-shrink-0 mt-0.5" />
                    <span className="text-sm dark:text-gray-300">{feature}</span>
                  </li>
                ))}
              </ul>

              {selectedPlan === plan.id && (
                <div className="absolute inset-0 rounded-lg ring-2 ring-orange-500 pointer-events-none"></div>
              )}
            </div>
          )
        })}
      </div>

      {/* Action Buttons */}
      <div className="flex items-center justify-between p-6 bg-gray-50 dark:bg-gray-800 rounded-lg">
        <div>
          {selectedPlan && (
            <div>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Selected: <span className="font-semibold dark:text-white">{plans.find(p => p.id === selectedPlan)?.name}</span>
                {' '}({billingCycle === 'yearly' ? 'Yearly' : 'Monthly'} billing)
              </p>
              {selectedPlan === 'professional' && (
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {userCount} user{userCount > 1 ? 's' : ''} •
                  {billingCycle === 'yearly'
                    ? ` $${(76.80 * userCount).toFixed(2)}/year`
                    : ` $${(8 * userCount).toFixed(2)}/month`
                  }
                </p>
              )}
            </div>
          )}
        </div>
        <div className="flex gap-4">
          <button
            onClick={() => router.push('/dashboard/billing')}
            className="px-6 py-2 border border-gray-300 dark:border-gray-600 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleUpgrade}
            disabled={!selectedPlan || isProcessing}
            className={`
              px-6 py-2 rounded-lg font-semibold transition-colors
              ${selectedPlan
                ? 'bg-orange-500 text-white hover:bg-orange-600'
                : 'bg-gray-300 text-gray-500 cursor-not-allowed'}
              disabled:opacity-50 disabled:cursor-not-allowed
            `}
          >
            {isProcessing ? (
              <span className="flex items-center">
                <svg className="animate-spin h-5 w-5 mr-2" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Processing...
              </span>
            ) : selectedPlan === 'enterprise' ? (
              'Contact Sales'
            ) : (
              'Continue to Checkout'
            )}
          </button>
        </div>
      </div>

      {/* Info Box */}
      <div className="mt-8 p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
        <h4 className="font-semibold text-blue-900 dark:text-blue-300 mb-2">Good to know</h4>
        <ul className="text-sm text-blue-800 dark:text-blue-400 space-y-1">
          <li>• All plans include a 14-day free trial</li>
          <li>• Cancel or change your plan anytime</li>
          <li>• {billingCycle === 'yearly' ? 'Save 20% with annual billing' : 'Switch to yearly billing and save 20%'}</li>
          <li>• Upgrades are prorated to your billing cycle</li>
          <li>• No setup fees or hidden charges</li>
        </ul>
      </div>
    </div>
  )
}
