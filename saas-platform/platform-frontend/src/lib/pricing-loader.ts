/**
 * Pricing configuration loader from YAML
 * This module loads and parses the pricing-config.yaml file
 */

import yaml from 'js-yaml'
import { PRICING_PLANS, type PlanId } from './pricing-config'

export interface YamlPricingConfig {
  product: {
    name: string
    description: string
    metadata: Record<string, string>
  }
  plans: Record<string, {
    name: string
    price_monthly: number
    price_yearly: number
    price_model?: string
    description: string
    stripe_price_id_monthly?: string
    stripe_price_id_yearly?: string
    recommended?: boolean
    features: string[]
    limits: {
      max_agents: number | 'unlimited'
      max_messages_per_day: number | 'unlimited'
      storage_gb: number | 'unlimited'
      support: string
      integrations: string
      workflows: boolean
      analytics: string
      sla: boolean
      training: boolean
      sso: boolean
      custom_development: boolean
      on_premise: boolean
      dedicated_infrastructure: boolean
    }
  }>
  trial: {
    enabled: boolean
    days: number
    applicable_plans: string[]
  }
  discounts: {
    annual_percentage: number
  }
}

// For now, we'll export a function that returns the current static config
// In production, this would fetch from the API or load the YAML directly
export async function loadPricingConfig(): Promise<typeof PRICING_PLANS> {
  // In a real implementation, this would:
  // 1. Fetch from /api/pricing which loads the YAML
  // 2. Or load the YAML directly if it's bundled with the frontend
  // For now, return the existing static config
  return PRICING_PLANS
}

// Helper to get Stripe price ID for a plan
export function getStripePriceId(plan: PlanId, billingCycle: 'monthly' | 'yearly' = 'monthly'): string | null {
  // This would be populated from the YAML config
  // For now, return null as prices aren't set up yet
  return null
}

// Helper to format price from cents
export function formatPrice(cents: number): string {
  return `$${(cents / 100).toFixed(2).replace(/\.00$/, '')}`
}
