// Type definitions for pricing plans
export type PlanId = 'free' | 'starter' | 'professional' | 'enterprise'

// Plan gradient colors for UI display
export const PLAN_GRADIENTS: Record<PlanId, string> = {
  free: 'from-gray-500 to-gray-600',
  starter: 'from-orange-500 to-orange-600',
  professional: 'from-purple-500 to-purple-600',
  enterprise: 'from-yellow-500 to-yellow-600',
}
