import type { PricingConfig } from '@/lib/api'

// Type definitions for pricing plans
export type PlanId = 'free' | 'byok' | 'hobby' | 'pro' | 'enterprise'

// Plans from smallest to largest; the no-plan `free` state has no place in it
const PLAN_ORDER: PlanId[] = ['byok', 'hobby', 'pro', 'enterprise']

/** A plan's position from smallest to largest, or -1 for a plan outside the order. */
export function planRank(plan: string): number {
  return PLAN_ORDER.indexOf(plan as PlanId)
}

/** Whether choosing `candidate` would move an account on `currentTier` to a smaller plan. */
export function isDowngrade(currentTier: PlanId | null, candidate: string): boolean {
  return currentTier !== null && planRank(candidate) < planRank(currentTier)
}

/** Days of free trial a customer's first plan starts with, or 0 when trials are off. */
export function trialDays(pricing: Pick<PricingConfig, 'trial'>): number {
  return pricing.trial.enabled ? pricing.trial.days : 0
}
