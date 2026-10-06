// Type definitions for pricing plans
export type PlanId = 'free' | 'byok' | 'hobby' | 'pro' | 'enterprise'

// Self-serve plans from smallest to largest, for upgrade and downgrade checks
export const PLAN_ORDER: PlanId[] = ['byok', 'hobby', 'pro', 'enterprise']
