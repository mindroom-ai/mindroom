import type { Subscription } from '@/hooks/useSubscription'
import type { PlanId } from '@/lib/pricing-config'

/**
 * Where an account stands with hosted plans: it never chose one (`none`), Stripe ended its plan so choosing a plan
 * starts a new subscription (`ended`), its plan cannot run an instance until billing is fixed in the Stripe portal
 * (`needs_billing`), or its plan runs one (`active`).
 */
export type PlanState = 'none' | 'ended' | 'needs_billing' | 'active'

type PlanFields = Pick<Subscription, 'tier' | 'can_run_instances' | 'stripe_subscription_ended'>

export function planState(subscription: PlanFields | null | undefined): PlanState {
  if (!subscription || subscription.tier === 'free') return 'none'
  if (subscription.can_run_instances) return 'active'
  return subscription.stripe_subscription_ended ? 'ended' : 'needs_billing'
}

/** The plan an account is still on, or null when it may choose any plan because it has none or Stripe ended it. */
export function currentPlanTier(subscription: PlanFields | null | undefined): PlanId | null {
  const state = planState(subscription)
  return subscription && (state === 'active' || state === 'needs_billing') ? subscription.tier : null
}

/** What an account whose plan cannot run an instance does next, as a sentence opener and a button. */
export function planAction(state: Exclude<PlanState, 'active'>): { step: string; label: string; href: string } {
  return state === 'none'
    ? { step: 'Choose a plan', label: 'Choose a plan', href: '/dashboard/billing/upgrade' }
    : { step: 'Add or restore billing', label: 'Open billing', href: '/dashboard/billing' }
}
