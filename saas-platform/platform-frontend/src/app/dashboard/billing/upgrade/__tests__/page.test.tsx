import { fireEvent, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom'
import UpgradePage from '../page'
import { getPricingConfig } from '@/lib/api'
import { useSubscription } from '@/hooks/useSubscription'

jest.mock('@/hooks/useSubscription', () => ({ useSubscription: jest.fn() }))
jest.mock('@/lib/api', () => ({ getPricingConfig: jest.fn(), createCheckoutSession: jest.fn() }))
jest.mock('@/lib/logger', () => ({ logger: { error: jest.fn() } }))

const plan = (name: string, recommended: boolean) => ({
  name,
  price_monthly: '$20',
  price_yearly: '$192',
  description: name,
  features: [],
  limits: { max_agents: 100, max_messages_per_day: 'unlimited', storage_gb: 10 },
  recommended,
  included_ai_budget_usd: 0,
  requires_customer_provider_keys: false,
  resource_profile: 'small',
})

const pricing = {
  product: { name: 'MindRoom', description: 'Hosted MindRoom', metadata: { platform: 'saas' } },
  plans: { hobby: plan('Hobby', true), pro: plan('Pro', false) },
  trial: { enabled: true, days: 3, applicable_plans: ['hobby', 'pro'] },
  discounts: { annual_percentage: 20 },
}

const lapsedHobby = () => ({ tier: 'hobby', status: 'cancelled', can_run_instances: false })

describe('UpgradePage', () => {
  beforeEach(() => {
    jest.clearAllMocks()
    ;(getPricingConfig as jest.Mock).mockResolvedValue(pricing)
    ;(useSubscription as jest.Mock).mockReturnValue({ subscription: lapsedHobby(), loading: false })
  })

  it('keeps the plan a lapsed customer chose when the subscription refreshes', async () => {
    const { rerender } = render(<UpgradePage />)

    expect(await screen.findByText(/Selected:/)).toHaveTextContent('Selected: Hobby')
    fireEvent.click(screen.getByRole('heading', { name: 'Pro' }))
    expect(screen.getByText(/Selected:/)).toHaveTextContent('Selected: Pro')

    ;(useSubscription as jest.Mock).mockReturnValue({ subscription: lapsedHobby(), loading: false })
    rerender(<UpgradePage />)

    expect(screen.getByText(/Selected:/)).toHaveTextContent('Selected: Pro')
  })

  it('does not promise a trial to a returning customer', async () => {
    render(<UpgradePage />)

    await screen.findByText(/Selected:/)
    expect(screen.queryByText(/free trial/)).not.toBeInTheDocument()
  })
})
