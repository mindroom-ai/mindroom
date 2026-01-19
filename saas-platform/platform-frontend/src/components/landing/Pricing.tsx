'use client'

import Link from 'next/link'
import { Check, X, Sparkles, Zap, Crown } from 'lucide-react'
import { useEffect, useState } from 'react'
import { PLAN_GRADIENTS } from '@/lib/pricing-config'

// Default pricing data - will be replaced at build time if API is available
const defaultPlans = [
  {
    name: 'Self-Hosted',
    icon: Sparkles,
    price: '$0',
    yearlyPrice: '$0',
    period: 'forever',
    description: 'Full control on your infrastructure',
    features: [
      { text: 'Unlimited AI Agents', included: true },
      { text: 'Unlimited messages', included: true },
      { text: 'Your own storage', included: true },
      { text: 'Community support', included: true },
      { text: 'All integrations', included: true },
      { text: 'Custom workflows', included: true },
      { text: 'Full data ownership', included: true },
      { text: 'Managed hosting', included: false },
    ],
    cta: 'View Docs',
    href: 'https://github.com/mindroom-ai/mindroom',
    featured: false,
    gradient: 'from-gray-500 to-gray-600',
  },
  {
    name: 'Starter',
    icon: Zap,
    price: '$10',
    yearlyPrice: '$96',
    period: '/month',
    description: 'Perfect for individuals',
    features: [
      { text: '100 AI Agents', included: true },
      { text: 'Unlimited messages', included: true },
      { text: '5GB storage', included: true },
      { text: 'Email support', included: true },
      { text: 'All integrations', included: true },
      { text: 'Custom workflows', included: true },
      { text: 'Analytics dashboard', included: true },
      { text: 'SSO & SAML', included: false },
    ],
    cta: 'Start 14-Day Trial',
    href: '/auth/signup?plan=starter',
    featured: true,
    gradient: PLAN_GRADIENTS.starter,
  },
  {
    name: 'Professional',
    icon: Crown,
    price: '$8',
    yearlyPrice: '$76.80',
    period: '/user/month',
    description: 'For teams and businesses',
    features: [
      { text: 'Unlimited AI Agents', included: true },
      { text: 'Unlimited messages', included: true },
      { text: '10GB storage per user', included: true },
      { text: 'Priority support', included: true },
      { text: 'Advanced analytics', included: true },
      { text: 'SSO & SAML', included: true },
      { text: 'SLA guarantee', included: true },
      { text: 'Team training', included: true },
    ],
    cta: 'Start 14-Day Trial',
    href: '/auth/signup?plan=professional',
    featured: false,
    gradient: PLAN_GRADIENTS.professional,
  },
]

const plans = defaultPlans

export function Pricing() {
  const [isVisible, setIsVisible] = useState(false)
  const [billingCycle, setBillingCycle] = useState<'monthly' | 'yearly'>('monthly')

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true)
        }
      },
      { threshold: 0.1 }
    )

    const element = document.getElementById('pricing')
    if (element) observer.observe(element)

    return () => {
      if (element) observer.unobserve(element)
    }
  }, [])

  return (
    <section id="pricing" className="py-20 md:py-24 px-6 relative overflow-hidden bg-gradient-to-b from-gray-50 to-white dark:from-gray-900 dark:to-gray-800">
      {/* Background decoration */}
      <div className="absolute top-0 right-0 w-96 h-96 bg-gradient-to-r from-orange-200 to-pink-200 dark:from-orange-900/10 dark:to-pink-900/10 rounded-full filter blur-3xl opacity-20"></div>
      <div className="absolute bottom-0 left-0 w-96 h-96 bg-gradient-to-r from-blue-200 to-purple-200 dark:from-blue-900/10 dark:to-purple-900/10 rounded-full filter blur-3xl opacity-20"></div>

      <div className="container mx-auto max-w-7xl relative z-10">
        <div className="text-center mb-12">
          <h2 className="text-4xl md:text-5xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Simple, Transparent Pricing
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300 max-w-3xl mx-auto mb-8">
            Start free, upgrade when you need more power. No hidden fees, no surprises.
          </p>

          {/* Billing toggle */}
          <div className="inline-flex items-center gap-2 p-1 bg-gray-100 dark:bg-gray-800 rounded-full">
            <button
              onClick={() => setBillingCycle('monthly')}
              className={`px-6 py-2.5 rounded-full font-medium transition-all duration-300 ${
                billingCycle === 'monthly'
                  ? 'bg-gradient-to-r from-orange-500 to-orange-600 text-white shadow-lg transform scale-105'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-white'
              }`}
            >
              Monthly
            </button>
            <button
              onClick={() => setBillingCycle('yearly')}
              className={`px-6 py-2.5 rounded-full font-medium transition-all duration-300 flex items-center gap-2 ${
                billingCycle === 'yearly'
                  ? 'bg-gradient-to-r from-orange-500 to-orange-600 text-white shadow-lg transform scale-105'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-white'
              }`}
            >
              Yearly
              <span className={`text-xs px-2 py-0.5 rounded-full transition-all duration-300 ${
                billingCycle === 'yearly'
                  ? 'bg-white/20 text-white'
                  : 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
              }`}>
                Save 20%
              </span>
            </button>
          </div>
        </div>

        <div className="grid lg:grid-cols-3 gap-8 max-w-6xl mx-auto">
          {plans.map((plan, index) => {
            const Icon = plan.icon
            const price = billingCycle === 'yearly' && plan.yearlyPrice ? plan.yearlyPrice : plan.price

            return (
              <div
                key={index}
                className={`relative bg-white dark:bg-gray-800 rounded-3xl overflow-hidden transition-all duration-500 ${
                  plan.featured
                    ? 'shadow-2xl scale-105 border-2 border-orange-500'
                    : 'shadow-xl hover:shadow-2xl border border-gray-200 dark:border-gray-700'
                } ${isVisible ? 'fade-in-up' : 'opacity-0'}`}
                style={{ animationDelay: `${index * 0.1}s` }}
              >
                {/* Featured badge */}
                {plan.featured && (
                  <div className="absolute top-0 right-0 bg-gradient-to-br from-orange-500 to-orange-600 text-white text-sm font-bold px-6 py-2 rounded-bl-2xl">
                    MOST POPULAR
                  </div>
                )}

                {/* Card content */}
                <div className="p-8">
                  {/* Icon and name */}
                  <div className="flex items-center gap-3 mb-6">
                    <div className={`w-12 h-12 bg-gradient-to-br ${plan.gradient} rounded-xl flex items-center justify-center`}>
                      <Icon className="w-6 h-6 text-white" />
                    </div>
                    <h3 className="text-2xl font-bold text-gray-900 dark:text-white">{plan.name}</h3>
                  </div>

                  {/* Price */}
                  <div className="mb-4">
                    <div className="flex items-baseline gap-2">
                      <span className="text-5xl font-bold text-gray-900 dark:text-white">{price}</span>
                      <span className="text-gray-600 dark:text-gray-400">
                        {plan.period === 'forever'
                          ? plan.period
                          : billingCycle === 'yearly'
                            ? plan.name === 'Professional' ? '/user/year' : '/year'
                            : plan.period}
                      </span>
                    </div>
                    <p className="text-gray-600 dark:text-gray-400 mt-2">{plan.description}</p>
                  </div>

                  {/* CTA Button */}
                  <Link
                    href={plan.href}
                    className={`block text-center py-3 px-6 rounded-full font-semibold mb-8 cursor-pointer ${
                      plan.featured
                        ? 'bg-gradient-to-r from-orange-500 to-orange-600 text-white transition-all duration-300 hover:from-orange-600 hover:to-orange-700 hover:shadow-2xl hover:shadow-orange-500/40 hover:-translate-y-1 transform'
                        : 'bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-white transition-all duration-300 hover:bg-gradient-to-r hover:from-gray-200 hover:to-gray-300 dark:hover:from-gray-600 dark:hover:to-gray-500 hover:shadow-2xl hover:-translate-y-1 transform'
                    }`}
                  >
                    {plan.cta}
                  </Link>

                  {/* Features list */}
                  <ul className="space-y-4">
                    {plan.features.map((feature, featureIndex) => (
                      <li key={featureIndex} className="flex items-start gap-3">
                        {feature.included ? (
                          <Check className="w-5 h-5 text-green-500 mt-0.5 flex-shrink-0" />
                        ) : (
                          <X className="w-5 h-5 text-gray-300 dark:text-gray-600 mt-0.5 flex-shrink-0" />
                        )}
                        <span className={feature.included ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400 dark:text-gray-600'}>
                          {feature.text}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )
          })}
        </div>

        {/* Enterprise CTA */}
        <div className="mt-16 text-center p-8 bg-gradient-to-r from-gray-900 to-gray-800 dark:from-gray-800 dark:to-gray-700 rounded-2xl max-w-4xl mx-auto">
          <h3 className="text-2xl font-bold text-white mb-3">Need an Enterprise Solution?</h3>
          <p className="text-gray-300 mb-6">
            Get custom AI agents, dedicated support, and enterprise-grade security for your organization.
          </p>
          <Link
            href="/contact"
            className="inline-flex items-center px-8 py-3 bg-white text-gray-900 font-semibold rounded-full hover:shadow-xl hover:scale-105 transition-all duration-300"
          >
            Contact Sales Team
          </Link>
        </div>
      </div>
    </section>
  )
}
