import { currentPlanTier, planAction, planState } from '../plan-state'

const plan = (can_run_instances: boolean, stripe_subscription_ended: boolean) =>
  ({ tier: 'hobby' as const, can_run_instances, stripe_subscription_ended })

describe('planState', () => {
  it('treats a missing subscription and the free tier as no plan', () => {
    expect(planState(null)).toBe('none')
    expect(planState(undefined)).toBe('none')
    expect(planState({ tier: 'free', can_run_instances: false, stripe_subscription_ended: true })).toBe('none')
  })

  it('separates a running plan, one Stripe ended, and one waiting on billing', () => {
    expect(planState(plan(true, false))).toBe('active')
    expect(planState(plan(false, true))).toBe('ended')
    expect(planState(plan(false, false))).toBe('needs_billing')
  })
})

describe('currentPlanTier', () => {
  it('keeps a running plan and one waiting on billing current, and frees the choice otherwise', () => {
    expect(currentPlanTier(plan(true, false))).toBe('hobby')
    expect(currentPlanTier(plan(false, false))).toBe('hobby')
    expect(currentPlanTier(plan(false, true))).toBeNull()
    expect(currentPlanTier(null)).toBeNull()
  })
})

describe('planAction', () => {
  it('sends an account without a plan to choosing one and a lapsed plan to billing', () => {
    expect(planAction('none')).toEqual({ step: 'Choose a plan', label: 'Choose a plan', href: '/dashboard/billing/upgrade' })
    expect(planAction('ended').href).toBe('/dashboard/billing')
    expect(planAction('needs_billing').label).toBe('Open billing')
  })
})
