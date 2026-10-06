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

const noPlanPricing = {
  ...enterprisePricing,
  plans: {
    free: {
      name: 'No plan',
      price_monthly: 0,
      price_yearly: 0,
      description: 'Choose a plan to run a hosted MindRoom instance',
      features: [],
      limits: { max_agents: 0, max_messages_per_day: 0, storage_gb: 0 },
      recommended: false,
      included_ai_budget_usd: 0,
      requires_customer_provider_keys: true,
      resource_profile: 'small',
    },
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

  it('shows an account without a plan as No plan with a way to choose one', async () => {
    ;(useSubscription as jest.Mock).mockReturnValue({
      subscription: { tier: 'free', status: 'active', stripe_subscription_id: null },
      loading: false,
      refresh: jest.fn(),
    })
    ;(getPricingConfig as jest.Mock).mockResolvedValue(noPlanPricing)

    render(<BillingPage />)

    await waitFor(() => {
      expect(screen.getByText('No plan')).toBeInTheDocument()
    })

    expect(screen.queryByText('$0/month')).not.toBeInTheDocument()
    expect(screen.queryByText('Active')).not.toBeInTheDocument()
    expect(screen.queryByText('Plan Includes:')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Choose a plan' })).toHaveAttribute('href', '/dashboard/billing/upgrade')
  })

  it('lets a lapsed plan holder choose their old plan again', async () => {
    ;(useSubscription as jest.Mock).mockReturnValue({
      subscription: { tier: 'hobby', status: 'cancelled', can_run_instances: false, stripe_subscription_id: null },
      loading: false,
      refresh: jest.fn(),
    })
    ;(getPricingConfig as jest.Mock).mockResolvedValue({
      ...enterprisePricing,
      plans: {
        byok: { ...enterprisePricing.plans.enterprise, name: 'Your own keys', price_monthly: '$10', price_yearly: '$96', features: [] },
        hobby: { ...enterprisePricing.plans.enterprise, name: 'Hobby', price_monthly: '$20', price_yearly: '$192', features: [] },
      },
    })

    render(<BillingPage />)

    expect(await screen.findByRole('button', { name: 'Choose Hobby' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Choose Your own keys' })).toBeInTheDocument()
    expect(screen.queryByText('Contact support to downgrade')).not.toBeInTheDocument()
  })
})
