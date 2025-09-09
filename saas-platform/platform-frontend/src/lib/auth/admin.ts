import { redirect } from 'next/navigation'
import { createServerClientSupabase } from '@/lib/supabase/server'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://api.staging.mindroom.chat'

export async function requireAdmin() {
  const supabase = await createServerClientSupabase()

  const { data: { user }, error } = await supabase.auth.getUser()

  if (error || !user) {
    console.error('[Admin Auth] Auth error:', error)
    redirect('/auth/login')
  }

  // Get session token for API call
  const { data: { session } } = await supabase.auth.getSession()

  if (!session) {
    redirect('/auth/login')
  }

  // Check admin status via API
  try {
    const response = await fetch(`${API_URL}/api/v1/account/is-admin`, {
      headers: {
        'Authorization': `Bearer ${session.access_token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok) {
      console.error('[Admin Auth] API error:', response.status)
      redirect('/dashboard')
    }

    const data = await response.json()

    if (!data.is_admin) {
      redirect('/dashboard')  // Redirect non-admins to regular dashboard
    }

    // Also get full account info
    const accountResponse = await fetch(`${API_URL}/api/v1/account/current`, {
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
    console.error('[Admin Auth] Request error:', err)
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
    const response = await fetch(`${API_URL}/api/v1/account/is-admin`, {
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
