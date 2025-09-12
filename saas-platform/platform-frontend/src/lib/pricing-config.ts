export type PlanId = 'free' | 'starter' | 'professional' | 'enterprise'

export interface PlanFeatures {
  maxAgents: number | 'unlimited'
  maxMessagesPerDay: number | 'unlimited'
  storageGb: number | 'unlimited'
  support: string
  integrations: string
  workflows: boolean
  analytics: 'basic' | 'advanced' | 'custom'
  sla: boolean
  training: boolean
  sso: boolean
  customDevelopment: boolean
  onPremise: boolean
  dedicatedInfrastructure: boolean
}

export interface Plan {
  id: PlanId
  name: string
  price: string
  period: string
  description: string
  features: string[]
  limits: PlanFeatures
  recommended?: boolean
  gradient?: string
}

export const PRICING_PLANS: Record<PlanId, Plan> = {
  free: {
    id: 'free',
    name: 'Free',
    price: '$0',
    period: '/month',
    description: 'Get started with basic features',
    features: [
      '1 AI Agent',
      '100 messages per day',
      '1GB storage',
      'Community support',
      'Basic integrations',
    ],
    limits: {
      maxAgents: 1,
      maxMessagesPerDay: 100,
      storageGb: 1,
      support: 'Community',
      integrations: 'Basic',
      workflows: false,
      analytics: 'basic',
      sla: false,
      training: false,
      sso: false,
      customDevelopment: false,
      onPremise: false,
      dedicatedInfrastructure: false,
    },
    gradient: 'from-gray-500 to-gray-600',
  },
  starter: {
    id: 'starter',
    name: 'Starter',
    price: '$10',
    period: '/month',
    description: 'Perfect for individuals',
    features: [
      '100 AI Agents',
      'Unlimited messages',
      '5GB storage',
      'Priority support',
      'All integrations',
      'Custom workflows',
      'Analytics dashboard',
    ],
    limits: {
      maxAgents: 100,
      maxMessagesPerDay: 'unlimited',
      storageGb: 5,
      support: 'Priority email',
      integrations: 'All integrations',
      workflows: true,
      analytics: 'advanced',
      sla: false,
      training: false,
      sso: false,
      customDevelopment: false,
      onPremise: false,
      dedicatedInfrastructure: false,
    },
    recommended: true,
    gradient: 'from-orange-500 to-orange-600',
  },
  professional: {
    id: 'professional',
    name: 'Professional',
    price: '$8',
    period: '/user/month',
    description: 'For teams and businesses',
    features: [
      'Unlimited AI Agents',
      'Unlimited messages',
      '10GB storage per user',
      'Priority support',
      'Advanced analytics',
      'SSO & SAML',
      'SLA guarantee',
      'Team training',
    ],
    limits: {
      maxAgents: 'unlimited',
      maxMessagesPerDay: 'unlimited',
      storageGb: 10,
      support: 'Priority support',
      integrations: 'All integrations + custom',
      workflows: true,
      analytics: 'advanced',
      sla: true,
      training: true,
      sso: true,
      customDevelopment: false,
      onPremise: false,
      dedicatedInfrastructure: false,
    },
    gradient: 'from-purple-500 to-purple-600',
  },
  enterprise: {
    id: 'enterprise',
    name: 'Enterprise',
    price: 'Custom',
    period: '',
    description: 'Tailored for large organizations',
    features: [
      'Unlimited everything',
      'Custom limits',
      'Dedicated infrastructure',
      'White-glove support',
      'Custom development',
      'On-premise option',
      'Compliance certifications',
      'Dedicated account manager',
    ],
    limits: {
      maxAgents: 'unlimited',
      maxMessagesPerDay: 'unlimited',
      storageGb: 'unlimited',
      support: 'White-glove',
      integrations: 'Custom development',
      workflows: true,
      analytics: 'custom',
      sla: true,
      training: true,
      sso: true,
      customDevelopment: true,
      onPremise: true,
      dedicatedInfrastructure: true,
    },
    gradient: 'from-yellow-500 to-yellow-600',
  },
}

// Helper function to get display values for limits
export function formatLimit(value: number | 'unlimited' | string): string {
  if (value === 'unlimited') return 'Unlimited'
  if (typeof value === 'number') {
    if (value >= 1000000) return `${value / 1000000}M`
    if (value >= 1000) return `${value / 1000}K`
    return value.toString()
  }
  return value
}

// For backward compatibility with existing tier display
export function getTierDisplay(tier: PlanId) {
  const plan = PRICING_PLANS[tier]
  return {
    name: plan.name,
    price: plan.price + (plan.period ? plan.period : ''),
    color: plan.gradient?.includes('orange') ? 'orange' :
           plan.gradient?.includes('purple') ? 'purple' :
           plan.gradient?.includes('yellow') ? 'yellow' : 'gray',
  }
}
