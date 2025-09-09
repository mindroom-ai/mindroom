import { createClient } from '@/lib/supabase/client'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://api.staging.mindroom.chat'

export async function apiCall(
  endpoint: string,
  options: RequestInit = {}
): Promise<Response> {
  const supabase = createClient()
  const { data: { session } } = await supabase.auth.getSession()

  return fetch(`${API_URL}${endpoint}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': session?.access_token ? `Bearer ${session.access_token}` : '',
      ...options.headers,
    },
  })
}

// Account Management
export async function getAccount() {
  const response = await apiCall('/api/v1/account/current')
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to fetch account')
  }
  return response.json()
}

export async function setupAccount() {
  const response = await apiCall('/api/v1/account/setup', { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to setup account')
  }
  return response.json()
}

// Instance Management
export async function listInstances() {
  const response = await apiCall('/api/v1/instances')
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to fetch instances')
  }
  return response.json()
}

export async function provisionInstance() {
  const response = await apiCall('/api/v1/instances/provision', { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to provision instance')
  }
  return response.json()
}

export async function startInstance(instanceId: string | number) {
  const response = await apiCall(`/api/v1/instances/${String(instanceId)}/start`, { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to start instance')
  }
  return response.json()
}

export async function stopInstance(instanceId: string | number) {
  const response = await apiCall(`/api/v1/instances/${String(instanceId)}/stop`, { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to stop instance')
  }
  return response.json()
}

export async function restartInstance(instanceId: string | number) {
  const response = await apiCall(`/api/v1/instances/${String(instanceId)}/restart`, { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to restart instance')
  }
  return response.json()
}

// Stripe Integration (to be added)
export async function createCheckoutSession(priceId: string, tier: string) {
  const response = await apiCall('/api/v1/stripe/checkout', {
    method: 'POST',
    body: JSON.stringify({ price_id: priceId, tier })
  })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to create checkout session')
  }
  return response.json()
}

export async function createPortalSession() {
  const response = await apiCall('/api/v1/stripe/portal', {
    method: 'POST'
  })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to create portal session')
  }
  return response.json()
}
