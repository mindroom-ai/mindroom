import type { Subscription } from '@/hooks/useSubscription'

/**
 * Where an account stands with hosted plans: it never chose one (`none`), its plan cannot run an
 * instance, for example after an expired trial or ended billing (`lapsed`), or its plan runs one (`active`).
 */
export type PlanState = 'none' | 'lapsed' | 'active'

export function planState(subscription: Pick<Subscription, 'tier' | 'can_run_instances'> | null | undefined): PlanState {
  if (!subscription || subscription.tier === 'free') return 'none'
  return subscription.can_run_instances ? 'active' : 'lapsed'
}
