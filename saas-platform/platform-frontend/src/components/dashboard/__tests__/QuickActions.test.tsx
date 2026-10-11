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
    can_run_instances: tier !== 'free',
    stripe_subscription_ended: tier === 'free',
    trial_days_remaining: null,
    current_period_start: null,
    current_period_end: null,
    trial_ends_at: null,
    cancelled_at: null,
    stripe_subscription_id: tier === 'free' ? null : 'sub_stripe',
    stripe_customer_id: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  }
}

describe('QuickActions', () => {
  it('asks an account without a plan to choose one and lists no plan limits', () => {
    render(<QuickActions subscription={subscription('free')} subscriptionLoading={false} />)

    expect(screen.getByText('Choose a plan to run a hosted instance')).toBeInTheDocument()
    expect(screen.queryByText('Plan Limits')).not.toBeInTheDocument()
    expect(screen.queryByText(/free plan/i)).not.toBeInTheDocument()
  })

  it('asks a lapsed plan holder to restore billing', () => {
    render(
      <QuickActions
        subscription={{ ...subscription('hobby'), status: 'cancelled', can_run_instances: false, stripe_subscription_ended: true }}
        subscriptionLoading={false}
      />
    )

    expect(screen.getByText('Add or restore billing to run a hosted instance')).toBeInTheDocument()
  })

  it('points a running plan at plan and billing management', () => {
    render(<QuickActions subscription={subscription('hobby')} subscriptionLoading={false} />)

    expect(screen.getByText('Your plan and billing')).toBeInTheDocument()
  })

  it('does not ask to choose a plan while the subscription loads', () => {
    render(<QuickActions subscription={null} subscriptionLoading />)

    expect(screen.getByText('Your plan and billing')).toBeInTheDocument()
  })

  it('links the documentation to the docs site', () => {
    render(<QuickActions subscription={subscription('hobby')} subscriptionLoading={false} />)

    expect(screen.getByRole('link', { name: /Documentation/ })).toHaveAttribute('href', 'https://docs.mindroom.chat/')
  })
})
