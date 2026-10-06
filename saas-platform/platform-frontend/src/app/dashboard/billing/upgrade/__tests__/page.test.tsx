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
  recommended,
  included_ai_budget_usd: 0,
  requires_customer_provider_keys: false,
  resource_profile: 'small',
})

const pricing = {
  product: { name: 'MindRoom', description: 'Hosted MindRoom', metadata: { platform: 'saas' } },
  plans: { byok: plan('Your own keys', false), hobby: plan('Hobby', true), pro: plan('Pro', false) },
  trial: { enabled: true, days: 3, applicable_plans: ['hobby', 'pro'] },
  discounts: { annual_percentage: 20 },
}

const lapsedHobby = () => ({ tier: 'hobby', status: 'cancelled', can_run_instances: false, stripe_subscription_ended: true })

describe('UpgradePage', () => {
  beforeEach(() => {
    jest.clearAllMocks()
    window.history.pushState({}, '', '/dashboard/billing/upgrade')
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

  it("preselects a lapsed customer's own plan rather than the recommended one", async () => {
    ;(useSubscription as jest.Mock).mockReturnValue({ subscription: { tier: 'byok', status: 'cancelled', can_run_instances: false, stripe_subscription_ended: true }, loading: false })

    render(<UpgradePage />)

    expect(await screen.findByText(/Selected:/)).toHaveTextContent('Selected: Your own keys')
  })

  it('preselects the plan named in the link', async () => {
    window.history.pushState({}, '', '/dashboard/billing/upgrade?plan=pro')

    render(<UpgradePage />)

    expect(await screen.findByText(/Selected:/)).toHaveTextContent('Selected: Pro')
  })

  it('preselects an upgrade named in the link for a running plan', async () => {
    window.history.pushState({}, '', '/dashboard/billing/upgrade?plan=pro')
    ;(useSubscription as jest.Mock).mockReturnValue({ subscription: { tier: 'hobby', status: 'active', can_run_instances: true, stripe_subscription_ended: false }, loading: false })

    render(<UpgradePage />)

    expect(await screen.findByText(/Selected:/)).toHaveTextContent('Selected: Pro')
  })

  it('keeps a plan with a billing problem current instead of offering it again', async () => {
    ;(useSubscription as jest.Mock).mockReturnValue({ subscription: { tier: 'hobby', status: 'unpaid', can_run_instances: false, stripe_subscription_ended: false }, loading: false })

    render(<UpgradePage />)

    expect(await screen.findByRole('heading', { name: 'Upgrade Your Plan' })).toBeInTheDocument()
    expect(screen.getByText('Currently on hobby plan. Upgrading will prorate your billing.')).toBeInTheDocument()
    expect(screen.queryByText(/Selected:/)).not.toBeInTheDocument()
  })

  it('does not promise a trial to a returning customer', async () => {
    render(<UpgradePage />)

    await screen.findByText(/Selected:/)
    expect(screen.queryByText(/free trial/)).not.toBeInTheDocument()
  })
})
