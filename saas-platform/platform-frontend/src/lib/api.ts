import { createClient } from '@/lib/supabase/client'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://api.staging.mindroom.chat'

export async function apiCall(
  endpoint: string,
  options: RequestInit = {}
): Promise<Response> {
  const supabase = createClient()
  const { data: { session } } = await supabase.auth.getSession()

  const url = `${API_URL}${endpoint}`
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
      console.log(`Request cancelled: ${url}`)
    } else {
      console.error(`API call failed: ${url}`, error)
    }
    throw error
  }
}

// Account Management
export async function getAccount() {
  const response = await apiCall('/my/account')
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to fetch account')
  }
  return response.json()
}

export async function setupAccount() {
  const response = await apiCall('/my/account/setup', { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to setup account')
  }
  return response.json()
}

// Instance Management
export async function listInstances() {
  const response = await apiCall('/my/instances')
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to fetch instances')
  }
  return response.json()
}

export async function provisionInstance() {
  const response = await apiCall('/my/instances/provision', { method: 'POST' })
  if (!response.ok) {
    let errorText = ''
    try {
      errorText = await response.text()
    } catch {
      // If we can't read the response (e.g., connection aborted), use generic message
      errorText = ''
    }
    throw new Error(errorText || 'Failed to provision instance')
  }
  return response.json()
}

export async function startInstance(instanceId: string | number) {
  const response = await apiCall(`/my/instances/${String(instanceId)}/start`, { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to start instance')
  }
  return response.json()
}

export async function stopInstance(instanceId: string | number) {
  const response = await apiCall(`/my/instances/${String(instanceId)}/stop`, { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to stop instance')
  }
  return response.json()
}

export async function restartInstance(instanceId: string | number) {
  const response = await apiCall(`/my/instances/${String(instanceId)}/restart`, { method: 'POST' })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to restart instance')
  }
  return response.json()
}

// Pricing
export async function getPricingConfig() {
  const response = await apiCall('/pricing/config')
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to fetch pricing configuration')
  }
  return response.json()
}

// Stripe Integration
export async function createCheckoutSession(tier: string, billingCycle: 'monthly' | 'yearly' = 'monthly', quantity: number = 1) {
  const response = await apiCall('/stripe/checkout', {
    method: 'POST',
    body: JSON.stringify({ tier, billing_cycle: billingCycle, quantity })
  })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to create checkout session')
  }
  return response.json()
}

export async function createPortalSession() {
  const response = await apiCall('/stripe/portal', {
    method: 'POST'
  })
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to create portal session')
  }
  return response.json()
}

// SSO cookie setup
export async function setSsoCookie() {
  const supabase = createClient()
  const { data: { session } } = await supabase.auth.getSession()
  if (!session?.access_token) return { ok: false }

  const response = await fetch(`${API_URL}/my/sso-cookie`, {
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
  await fetch(`${API_URL}/my/sso-cookie`, {
    method: 'DELETE',
    credentials: 'include',
  })
}
