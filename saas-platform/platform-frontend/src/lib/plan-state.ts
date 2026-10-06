import type { Subscription } from '@/hooks/useSubscription'

/**
 * Where an account stands with hosted plans: it never chose one (`none`), its plan cannot run an
 * instance, for example after an expired trial or ended billing (`lapsed`), or its plan runs one (`active`).
 */
export type PlanState = 'none' | 'lapsed' | 'active'

/**
 * Whether a lapsed plan's Stripe subscription is over, so choosing a plan starts a new one.
 * A past-due, unpaid, paused, or incomplete subscription is fixed in the Stripe portal instead.
 */
export function lapsedPlanEnded(subscription: Pick<Subscription, 'status' | 'stripe_subscription_id'>): boolean {
  return !subscription.stripe_subscription_id || subscription.status === 'cancelled' || subscription.status === 'incomplete_expired'
}

export function planState(subscription: Pick<Subscription, 'tier' | 'can_run_instances'> | null | undefined): PlanState {
  if (!subscription || subscription.tier === 'free') return 'none'
  return subscription.can_run_instances ? 'active' : 'lapsed'
}
