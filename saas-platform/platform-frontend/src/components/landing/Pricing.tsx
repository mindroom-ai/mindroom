import Link from 'next/link'
import { Check } from 'lucide-react'

const plans = [
  {
    name: 'Free',
    price: '$0',
    period: 'forever',
    description: 'Perfect for trying out MindRoom',
    features: [
      '1 AI Agent',
      '100 messages/day',
      '1GB storage',
      'Community support',
      'Basic integrations',
    ],
    cta: 'Start Free',
    href: '/auth/signup',
    featured: false,
  },
  {
    name: 'Starter',
    price: '$49',
    period: '/month',
    description: 'Great for small teams',
    features: [
      '5 AI Agents',
      '5,000 messages/day',
      '10GB storage',
      'Priority support',
      'All integrations',
      'Custom workflows',
      'Analytics dashboard',
    ],
    cta: 'Start Trial',
    href: '/auth/signup?plan=starter',
    featured: true,
  },
  {
    name: 'Professional',
    price: '$199',
    period: '/month',
    description: 'For growing businesses',
    features: [
      'Unlimited AI Agents',
      '50,000 messages/day',
      '100GB storage',
      '24/7 phone support',
      'Advanced analytics',
      'Custom integrations',
      'SLA guarantee',
      'Team training',
    ],
    cta: 'Start Trial',
    href: '/auth/signup?plan=professional',
    featured: false,
  },
]

export function Pricing() {
  return (
    <section className="py-20 px-6 dark:bg-gray-900">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-12">
          <h2 className="text-4xl font-bold mb-4 dark:text-white">
            Simple, Transparent Pricing
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300">
            Start free, upgrade when you need more power
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-8">
          {plans.map((plan, index) => (
            <div
              key={index}
              className={`bg-white dark:bg-gray-800 rounded-2xl p-8 ${
                plan.featured
                  ? 'ring-2 ring-orange-500 shadow-xl scale-105'
                  : 'shadow-lg'
              }`}
            >
              {plan.featured && (
                <div className="text-center mb-4">
                  <span className="bg-orange-500 text-white text-sm font-medium px-3 py-1 rounded-full">
                    Most Popular
                  </span>
                </div>
              )}

              <div className="text-center mb-8">
                <h3 className="text-2xl font-bold mb-2 dark:text-white">{plan.name}</h3>
                <div className="flex items-baseline justify-center gap-1 mb-2">
                  <span className="text-4xl font-bold dark:text-white">{plan.price}</span>
                  <span className="text-gray-600 dark:text-gray-400">{plan.period}</span>
                </div>
                <p className="text-gray-600 dark:text-gray-400">{plan.description}</p>
              </div>

              <ul className="space-y-3 mb-8">
                {plan.features.map((feature, featureIndex) => (
                  <li key={featureIndex} className="flex items-start gap-3">
                    <Check className="w-5 h-5 text-green-500 mt-0.5" />
                    <span className="text-gray-700 dark:text-gray-300">{feature}</span>
                  </li>
                ))}
              </ul>

              <Link
                href={plan.href}
                className={`block text-center py-3 px-6 rounded-lg font-medium transition-colors ${
                  plan.featured
                    ? 'bg-orange-500 text-white hover:bg-orange-600'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-white hover:bg-gray-200 dark:hover:bg-gray-600'
                }`}
              >
                {plan.cta}
              </Link>
            </div>
          ))}
        </div>

        <div className="text-center mt-12">
          <p className="text-gray-600 dark:text-gray-400">
            Need a custom plan?{' '}
            <Link href="/contact" className="text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300 font-medium">
              Contact our sales team
            </Link>
          </p>
        </div>
      </div>
    </section>
  )
}
