'use client'

import { useState } from 'react'
import { Check, X } from 'lucide-react'
import { useRouter } from 'next/navigation'
import { createCheckoutSession } from '@/lib/api'

type PricingTier = {
  id: string
  name: string
  price: string
  priceId: string
  description: string
  features: string[]
  limitations: string[]
  highlighted?: boolean
}

const pricingTiers: PricingTier[] = [
  {
    id: 'free',
    name: 'Free',
    price: '$0',
    priceId: '',
    description: 'Perfect for trying out MindRoom',
    features: [
      '1 AI Agent',
      '100 messages per day',
      '1GB storage',
      'Community support',
      'Basic integrations',
    ],
    limitations: [
      'No custom agents',
      'No API access',
      'No priority support',
    ],
  },
  {
    id: 'starter',
    name: 'Starter',
    price: '$49',
    priceId: process.env.STRIPE_PRICE_STARTER || '',
    description: 'Great for individuals and small teams',
    features: [
      '5 AI Agents',
      '5,000 messages per day',
      '10GB storage',
      'Priority support',
      'All integrations',
      'Custom workflows',
      'Analytics dashboard',
      'API access',
    ],
    limitations: [
      'No SLA guarantee',
      'No custom integrations',
    ],
    highlighted: true,
  },
  {
    id: 'professional',
    name: 'Professional',
    price: '$199',
    priceId: process.env.STRIPE_PRICE_PROFESSIONAL || '',
    description: 'For growing businesses',
    features: [
      'Unlimited AI Agents',
      '50,000 messages per day',
      '100GB storage',
      '24/7 phone support',
      'Advanced analytics',
      'Custom integrations',
      'SLA guarantee',
      'Team training',
      'White-label options',
      'Advanced memory',
      'Voice messages',
    ],
    limitations: [],
  },
  {
    id: 'enterprise',
    name: 'Enterprise',
    price: 'Custom',
    priceId: '',
    description: 'For large organizations',
    features: [
      'Unlimited everything',
      'Custom limits',
      'Dedicated infrastructure',
      'White-glove support',
      'Custom development',
      'On-premise option',
      'Compliance certifications',
      'Dedicated account manager',
      'Custom SLA',
      'Priority roadmap input',
    ],
    limitations: [],
  },
]

export default function PricingPage() {
  const router = useRouter()
  const [loading, setLoading] = useState<string | null>(null)

  const handleSelectPlan = async (tier: PricingTier) => {
    if (tier.id === 'free') {
      router.push('/auth/signup')
      return
    }

    if (tier.id === 'enterprise') {
      window.location.href = 'mailto:sales@mindroom.chat?subject=Enterprise Plan Inquiry'
      return
    }

    setLoading(tier.id)

    try {
      const { url } = await createCheckoutSession(tier.priceId, tier.id)
      window.location.href = url
    } catch (error) {
      console.error('Error creating checkout session:', error)
      alert('An error occurred. Please try again.')
      setLoading(null)
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-orange-50 to-yellow-50">
      <div className="max-w-7xl mx-auto px-4 py-16 sm:px-6 lg:px-8">
        {/* Header */}
        <div className="text-center mb-16">
          <h1 className="text-4xl font-bold text-gray-900 mb-4">
            Choose Your MindRoom Plan
          </h1>
          <p className="text-xl text-gray-600 max-w-2xl mx-auto">
            Scale your AI workforce with flexible pricing that grows with your business
          </p>
        </div>

        {/* Pricing Grid */}
        <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-8">
          {pricingTiers.map((tier) => (
            <div
              key={tier.id}
              className={`
                relative bg-white rounded-2xl shadow-lg p-8
                ${tier.highlighted ? 'ring-2 ring-orange-500 scale-105' : ''}
                hover:shadow-xl transition-shadow
              `}
            >
              {tier.highlighted && (
                <div className="absolute -top-4 left-1/2 transform -translate-x-1/2">
                  <span className="bg-orange-500 text-white px-4 py-1 rounded-full text-sm font-semibold">
                    Most Popular
                  </span>
                </div>
              )}

              <div className="mb-8">
                <h3 className="text-2xl font-bold text-gray-900 mb-2">
                  {tier.name}
                </h3>
                <p className="text-gray-600 text-sm mb-4">
                  {tier.description}
                </p>
                <div className="flex items-baseline">
                  <span className="text-4xl font-bold text-gray-900">
                    {tier.price}
                  </span>
                  {tier.price !== 'Custom' && (
                    <span className="text-gray-600 ml-2">/month</span>
                  )}
                </div>
              </div>

              {/* Features */}
              <div className="mb-8">
                <h4 className="text-sm font-semibold text-gray-900 mb-4">
                  Includes:
                </h4>
                <ul className="space-y-3">
                  {tier.features.map((feature, index) => (
                    <li key={index} className="flex items-start">
                      <Check className="w-5 h-5 text-green-500 mr-2 flex-shrink-0 mt-0.5" />
                      <span className="text-sm text-gray-700">{feature}</span>
                    </li>
                  ))}
                </ul>
              </div>

              {/* Limitations */}
              {tier.limitations.length > 0 && (
                <div className="mb-8">
                  <ul className="space-y-2">
                    {tier.limitations.map((limitation, index) => (
                      <li key={index} className="flex items-start">
                        <X className="w-5 h-5 text-gray-400 mr-2 flex-shrink-0 mt-0.5" />
                        <span className="text-sm text-gray-500">
                          {limitation}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* CTA Button */}
              <button
                onClick={() => handleSelectPlan(tier)}
                disabled={loading === tier.id}
                className={`
                  w-full py-3 px-4 rounded-lg font-semibold transition-colors
                  ${
                    tier.highlighted
                      ? 'bg-orange-500 text-white hover:bg-orange-600'
                      : 'bg-gray-100 text-gray-900 hover:bg-gray-200'
                  }
                  disabled:opacity-50 disabled:cursor-not-allowed
                `}
              >
                {loading === tier.id ? (
                  <span className="flex items-center justify-center">
                    <svg
                      className="animate-spin h-5 w-5 mr-2"
                      xmlns="http://www.w3.org/2000/svg"
                      fill="none"
                      viewBox="0 0 24 24"
                    >
                      <circle
                        className="opacity-25"
                        cx="12"
                        cy="12"
                        r="10"
                        stroke="currentColor"
                        strokeWidth="4"
                      />
                      <path
                        className="opacity-75"
                        fill="currentColor"
                        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                      />
                    </svg>
                    Processing...
                  </span>
                ) : tier.id === 'enterprise' ? (
                  'Contact Sales'
                ) : tier.id === 'free' ? (
                  'Start Free'
                ) : (
                  'Get Started'
                )}
              </button>
            </div>
          ))}
        </div>

        {/* FAQ Section */}
        <div className="mt-20">
          <h2 className="text-3xl font-bold text-center mb-12">
            Frequently Asked Questions
          </h2>
          <div className="grid md:grid-cols-2 gap-8 max-w-4xl mx-auto">
            <div>
              <h3 className="font-semibold text-lg mb-2">
                Can I change plans later?
              </h3>
              <p className="text-gray-600">
                Yes, you can upgrade or downgrade your plan at any time. Changes take effect immediately.
              </p>
            </div>
            <div>
              <h3 className="font-semibold text-lg mb-2">
                Do you offer a free trial?
              </h3>
              <p className="text-gray-600">
                Yes, all paid plans come with a 14-day free trial. No credit card required.
              </p>
            </div>
            <div>
              <h3 className="font-semibold text-lg mb-2">
                What happens if I exceed my limits?
              </h3>
              <p className="text-gray-600">
                We'll notify you when you're approaching your limits and offer options to upgrade.
              </p>
            </div>
            <div>
              <h3 className="font-semibold text-lg mb-2">
                Can I cancel anytime?
              </h3>
              <p className="text-gray-600">
                Yes, you can cancel your subscription at any time with no penalties.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
