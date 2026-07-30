import { render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import BillingPage from '../page'
import { getPricingConfig } from '@/lib/api'
import { useSubscription } from '@/hooks/useSubscription'

jest.mock('@/hooks/useSubscription', () => ({
  useSubscription: jest.fn(),
}))

jest.mock('@/lib/api', () => ({
  createPortalSession: jest.fn(),
  getPricingConfig: jest.fn(),
}))

jest.mock('@/lib/logger', () => ({
  logger: {
    error: jest.fn(),
  },
}))

const enterprisePricing = {
  product: {
    name: 'MindRoom',
    description: 'Hosted MindRoom',
    metadata: { platform: 'saas' },
  },
  plans: {
    enterprise: {
      name: 'Enterprise',
      price_monthly: 'custom',
      price_yearly: 'custom',
      description: 'Custom enterprise plan',
      features: ['Dedicated support'],
      limits: {
        max_agents: 'unlimited',
        max_messages_per_day: 'unlimited',
        storage_gb: 'unlimited',
      },
      recommended: false,
      included_ai_budget_usd: 0,
      requires_customer_provider_keys: false,
      resource_profile: 'pro',
    },
  },
  trial: {
    enabled: false,
    days: 0,
    applicable_plans: [],
  },
  discounts: {
    annual_percentage: 20,
  },
}

describe('BillingPage', () => {
  beforeEach(() => {
    jest.clearAllMocks()
    ;(useSubscription as jest.Mock).mockReturnValue({
      subscription: {
        tier: 'enterprise',
        status: 'active',
        stripe_subscription_id: null,
      },
      loading: false,
      refresh: jest.fn(),
    })
    ;(getPricingConfig as jest.Mock).mockResolvedValue(enterprisePricing)
  })

  it('renders enterprise custom pricing without a monthly suffix', async () => {
    render(<BillingPage />)

    await waitFor(() => {
      expect(screen.getByText('Enterprise')).toBeInTheDocument()
    })

    expect(screen.getByText('Custom')).toBeInTheDocument()
    expect(screen.queryByText('custom/month')).not.toBeInTheDocument()
  })
})
