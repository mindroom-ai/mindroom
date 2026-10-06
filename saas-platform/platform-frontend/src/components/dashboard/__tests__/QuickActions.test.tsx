import { render, screen } from '@testing-library/react'
import '@testing-library/jest-dom'
import { QuickActions } from '../QuickActions'
import type { Subscription } from '@/hooks/useSubscription'

function subscription(tier: Subscription['tier']): Subscription {
  return {
    id: 'sub-1',
    account_id: 'acc-1',
    tier,
    status: 'active',
    max_agents: 0,
    max_messages_per_day: 0,
    max_storage_gb: 0,
    can_run_instances: tier !== 'free',
    trial_days_remaining: null,
    current_period_start: null,
    current_period_end: null,
    trial_ends_at: null,
    cancelled_at: null,
    stripe_subscription_id: null,
    stripe_customer_id: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  }
}

describe('QuickActions', () => {
  it('asks an account without a plan to choose one and lists no plan limits', () => {
    render(<QuickActions subscription={subscription('free')} />)

    expect(screen.getByText('Choose a plan to run a hosted instance')).toBeInTheDocument()
    expect(screen.queryByText('Plan Limits')).not.toBeInTheDocument()
    expect(screen.queryByText(/free plan/i)).not.toBeInTheDocument()
  })

  it('asks a lapsed plan holder to restore billing', () => {
    render(<QuickActions subscription={{ ...subscription('hobby'), status: 'cancelled', can_run_instances: false }} />)

    expect(screen.getByText('Restore billing for your hobby plan')).toBeInTheDocument()
  })

  it('names the current paid plan', () => {
    render(<QuickActions subscription={subscription('hobby')} />)

    expect(screen.getByText('Current: hobby plan')).toBeInTheDocument()
  })

  it('links the documentation to the docs site', () => {
    render(<QuickActions subscription={subscription('hobby')} />)

    expect(screen.getByRole('link', { name: /Documentation/ })).toHaveAttribute('href', 'https://docs.mindroom.chat/')
  })
})
