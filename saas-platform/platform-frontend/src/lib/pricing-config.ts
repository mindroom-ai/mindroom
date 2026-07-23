// Type definitions for pricing plans
export type PlanId = 'free' | 'byok' | 'hobby' | 'pro' | 'enterprise'

// Plan gradient colors for UI display
export const PLAN_GRADIENTS: Record<PlanId, string> = {
  free: 'from-gray-500 to-gray-600',
  byok: 'from-slate-500 to-slate-700',
  hobby: 'from-orange-500 to-orange-600',
  pro: 'from-purple-500 to-purple-600',
  enterprise: 'from-yellow-500 to-yellow-600',
}
