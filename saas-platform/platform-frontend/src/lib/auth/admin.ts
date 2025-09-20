import { redirect } from 'next/navigation'
import { getServerRuntimeConfig } from '@/lib/runtime-config'
import { createServerClientSupabase } from '@/lib/supabase/server'
import { logger } from '../logger'

export async function requireAdmin() {
  const supabase = await createServerClientSupabase()

  const { data: { user }, error } = await supabase.auth.getUser()

  if (error || !user) {
    logger.error('[Admin Auth] Auth error:', error)
    redirect('/auth/login')
  }

  // Get session token for API call
  const { data: { session } } = await supabase.auth.getSession()

  if (!session) {
    redirect('/auth/login')
  }

  // Check admin status via API
  const { apiUrl } = getServerRuntimeConfig()

  try {
    const response = await fetch(`${apiUrl}/my/account/admin-status`, {
      headers: {
        'Authorization': `Bearer ${session.access_token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok) {
      logger.error('[Admin Auth] API error:', response.status)
      redirect('/dashboard')
    }

    const data = await response.json()

    if (!data.is_admin) {
      redirect('/dashboard')  // Redirect non-admins to regular dashboard
    }

    // Also get full account info
    const accountResponse = await fetch(`${apiUrl}/my/account`, {
      headers: {
        'Authorization': `Bearer ${session.access_token}`,
        'Content-Type': 'application/json',
      },
    })

    if (accountResponse.ok) {
      const account = await accountResponse.json()
      return { user, account }
    }

    return { user, account: { id: user.id, email: user.email, is_admin: true } }
  } catch (err) {
    logger.error('[Admin Auth] Request error:', err)
    redirect('/dashboard')
  }
}

export async function isAdmin() {
  const supabase = await createServerClientSupabase()

  const { data: { user }, error } = await supabase.auth.getUser()

  if (error || !user) {
    return false
  }

  // Get session token for API call
  const { data: { session } } = await supabase.auth.getSession()

  if (!session) {
    return false
  }

  try {
    const response = await fetch(`${API_URL}/my/account/admin-status`, {
      headers: {
        'Authorization': `Bearer ${session.access_token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok) {
      return false
    }

    const data = await response.json()
    return data.is_admin === true
  } catch {
    return false
  }
}
