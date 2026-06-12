import { getRuntimeConfig } from '@/lib/runtime-config'
import { createClient } from '@/lib/supabase/client'
import { logger } from './logger'
import type { components } from './api.generated'

type Schemas = components['schemas']

// Response types shared with the FastAPI backend (regenerate with `bun run generate:api-types`).
export type Account = Schemas['AccountWithRelationsOut']
export type AccountSetupResponse = Schemas['AccountSetupResponse']
export type GdprExport = Schemas['GdprExportResponse']
export type GdprDeletionResponse = Schemas['GdprDeletionResponse']
export type GdprCancelDeletionResponse = Schemas['GdprCancelDeletionResponse']
export type GdprConsentResponse = Schemas['GdprConsentResponse']
export type Instance = Schemas['InstanceOut']
export type InstancesResponse = Schemas['InstancesResponse']
export type ProvisionResponse = Schemas['ProvisionResponse']
export type ActionResult = Schemas['ActionResult']
export type PricingConfig = Schemas['PricingConfigResponse']
export type PricingPlan = Schemas['PricingPlanOut']
export type UrlResponse = Schemas['UrlResponse']

const resolveApiUrl = () => getRuntimeConfig().apiUrl

export async function apiCall(
  endpoint: string,
  options: RequestInit = {}
): Promise<Response> {
  const apiUrl = resolveApiUrl()
  const supabase = createClient()
  const { data: { session } } = await supabase.auth.getSession()

  const url = `${apiUrl}${endpoint}`
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': session?.access_token ? `Bearer ${session.access_token}` : '',
    ...options.headers,
  }

  try {
    return await fetch(url, {
      ...options,
      headers,
    })
  } catch (error: any) {
    // Log the error but check if it's a cancellation
    if (error?.name === 'AbortError' || !error?.message) {
      logger.log(`Request cancelled: ${url}`)
    } else if (error?.message?.includes('CORS') || error?.message?.includes('NetworkError')) {
      logger.error(`CORS/Network error - Backend may need restart or CORS configuration: ${url}`, error)
      throw new Error(`Cannot connect to backend. Please ensure the backend is running and CORS is configured for ${window.location.origin}`)
    } else {
      logger.error(`API call failed: ${url}`, error)
    }
    throw error
  }
}

async function request<T>(
  endpoint: string,
  errorMessage: string,
  options: RequestInit = {}
): Promise<T> {
  const response = await apiCall(endpoint, options)
  if (!response.ok) {
    let errorText = ''
    try {
      errorText = await response.text()
    } catch {
      // If we can't read the response (e.g., connection aborted), use the generic message
      errorText = ''
    }
    throw new Error(errorText || errorMessage)
  }
  return response.json() as Promise<T>
}

// Account Management
export async function getAccount(): Promise<Account> {
  return request('/my/account', 'Failed to fetch account')
}

export async function setupAccount(): Promise<AccountSetupResponse> {
  return request('/my/account/setup', 'Failed to setup account', { method: 'POST' })
}

// GDPR Endpoints
export async function exportUserData(): Promise<GdprExport> {
  return request('/my/gdpr/export-data', 'Failed to export data')
}

export async function requestAccountDeletion(confirmation: boolean = false): Promise<GdprDeletionResponse> {
  const body: Schemas['DeletionRequest'] = { confirmation }
  return request('/my/gdpr/request-deletion', 'Failed to request deletion', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function cancelAccountDeletion(): Promise<GdprCancelDeletionResponse> {
  return request('/my/gdpr/cancel-deletion', 'Failed to cancel deletion', { method: 'POST' })
}

export async function updateConsent(marketing: boolean, analytics: boolean): Promise<GdprConsentResponse> {
  const body: Schemas['ConsentUpdate'] = { marketing, analytics }
  return request('/my/gdpr/consent', 'Failed to update consent', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// Instance Management
export async function listInstances(): Promise<InstancesResponse> {
  return request('/my/instances', 'Failed to fetch instances')
}

export async function provisionInstance(): Promise<ProvisionResponse> {
  return request('/my/instances/provision', 'Failed to provision instance', { method: 'POST' })
}

export async function startInstance(instanceId: string | number): Promise<ActionResult> {
  return request(`/my/instances/${String(instanceId)}/start`, 'Failed to start instance', { method: 'POST' })
}

export async function stopInstance(instanceId: string | number): Promise<ActionResult> {
  return request(`/my/instances/${String(instanceId)}/stop`, 'Failed to stop instance', { method: 'POST' })
}

export async function restartInstance(instanceId: string | number): Promise<ActionResult> {
  return request(`/my/instances/${String(instanceId)}/restart`, 'Failed to restart instance', { method: 'POST' })
}

// Pricing
export async function getPricingConfig(): Promise<PricingConfig> {
  return request('/pricing/config', 'Failed to fetch pricing configuration')
}

// Stripe Integration
export async function createCheckoutSession(
  tier: string,
  billingCycle: 'monthly' | 'yearly' = 'monthly'
): Promise<UrlResponse> {
  const body: Schemas['CheckoutRequest'] = { tier, billing_cycle: billingCycle }
  return request('/stripe/checkout', 'Failed to create checkout session', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function createPortalSession(): Promise<UrlResponse> {
  return request('/stripe/portal', 'Failed to create portal session', { method: 'POST' })
}

// SSO cookie setup
export async function setSsoCookie() {
  const apiUrl = resolveApiUrl()
  const supabase = createClient()
  const { data: { session } } = await supabase.auth.getSession()
  if (!session?.access_token) return { ok: false }

  const response = await fetch(`${apiUrl}/my/sso-cookie`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${session.access_token}`,
    },
  })
  return { ok: response.ok }
}

export async function clearSsoCookie() {
  const apiUrl = resolveApiUrl()
  await fetch(`${apiUrl}/my/sso-cookie`, {
    method: 'DELETE',
    credentials: 'include',
  })
}
