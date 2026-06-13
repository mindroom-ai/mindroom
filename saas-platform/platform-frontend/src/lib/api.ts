import { getRuntimeConfig } from '@/lib/runtime-config'
import { createClient } from '@/lib/supabase/client'
import { logger } from './logger'
import type { paths } from './api.generated'

const resolveApiUrl = () => getRuntimeConfig().apiUrl

// Extract the 200 application/json payload (or request body) of a generated route.
type SuccessJson<Path extends keyof paths, Method extends keyof paths[Path]> =
  paths[Path][Method] extends { responses: { 200: { content: { 'application/json': infer R } } } } ? R : never

type RequestJson<Path extends keyof paths, Method extends keyof paths[Path]> =
  paths[Path][Method] extends { requestBody: { content: { 'application/json': infer B } } } ? B : never

export type Account = SuccessJson<'/my/account', 'get'>
export type AccountSetup = SuccessJson<'/my/account/setup', 'post'>
export type GdprExport = SuccessJson<'/my/gdpr/export-data', 'get'>
export type GdprDeletion = SuccessJson<'/my/gdpr/request-deletion', 'post'>
export type GdprCancelDeletion = SuccessJson<'/my/gdpr/cancel-deletion', 'post'>
export type GdprConsent = SuccessJson<'/my/gdpr/consent', 'post'>
export type Instances = SuccessJson<'/my/instances', 'get'>
export type Instance = Instances['instances'][number]
export type Provision = SuccessJson<'/my/instances/provision', 'post'>
export type PricingConfig = SuccessJson<'/pricing/config', 'get'>

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
  fallbackError: string,
  options: RequestInit = {}
): Promise<T> {
  const response = await apiCall(endpoint, options)
  if (!response.ok) {
    let detail = ''
    try {
      detail = await response.text()
    } catch {
      // If we can't read the response (e.g., connection aborted), use the fallback
      detail = ''
    }
    throw new Error(detail || fallbackError)
  }
  return response.json()
}

// Account Management
export async function getAccount(): Promise<Account> {
  return request('/my/account', 'Failed to fetch account')
}

export async function setupAccount(): Promise<AccountSetup> {
  return request('/my/account/setup', 'Failed to setup account', { method: 'POST' })
}

// GDPR Endpoints
export async function exportUserData(): Promise<GdprExport> {
  return request('/my/gdpr/export-data', 'Failed to export data')
}

export async function requestAccountDeletion(confirmation: boolean = false): Promise<GdprDeletion> {
  const body: RequestJson<'/my/gdpr/request-deletion', 'post'> = { confirmation }
  return request('/my/gdpr/request-deletion', 'Failed to request deletion', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function cancelAccountDeletion(): Promise<GdprCancelDeletion> {
  return request('/my/gdpr/cancel-deletion', 'Failed to cancel deletion', { method: 'POST' })
}

export async function updateConsent(marketing: boolean, analytics: boolean): Promise<GdprConsent> {
  const body: RequestJson<'/my/gdpr/consent', 'post'> = { marketing, analytics }
  return request('/my/gdpr/consent', 'Failed to update consent', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// Instance Management
export async function listInstances(): Promise<Instances> {
  return request('/my/instances', 'Failed to fetch instances')
}

export async function provisionInstance(): Promise<Provision> {
  return request('/my/instances/provision', 'Failed to provision instance', { method: 'POST' })
}

export async function startInstance(
  instanceId: string | number
): Promise<SuccessJson<'/my/instances/{instance_id}/start', 'post'>> {
  return request(`/my/instances/${String(instanceId)}/start`, 'Failed to start instance', { method: 'POST' })
}

export async function stopInstance(
  instanceId: string | number
): Promise<SuccessJson<'/my/instances/{instance_id}/stop', 'post'>> {
  return request(`/my/instances/${String(instanceId)}/stop`, 'Failed to stop instance', { method: 'POST' })
}

export async function restartInstance(
  instanceId: string | number
): Promise<SuccessJson<'/my/instances/{instance_id}/restart', 'post'>> {
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
): Promise<SuccessJson<'/stripe/checkout', 'post'>> {
  const body: RequestJson<'/stripe/checkout', 'post'> = { tier, billing_cycle: billingCycle }
  return request('/stripe/checkout', 'Failed to create checkout session', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function createPortalSession(): Promise<SuccessJson<'/stripe/portal', 'post'>> {
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
