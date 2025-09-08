import { redirect } from 'next/navigation'
import { createServerClientSupabase } from '@/lib/supabase/server'
import { createClient } from '@supabase/supabase-js'

export async function requireAdmin() {
  const supabase = await createServerClientSupabase()

  const { data: { user }, error } = await supabase.auth.getUser()

  if (error || !user) {
    console.error('[Admin Auth] Auth error:', error)
    redirect('/auth/login')
  }

  // First try with regular client
  let { data: account, error: accountError } = await supabase
    .from('accounts')
    .select('*')  // Select all fields to ensure we get is_admin
    .eq('id', user.id)
    .single()

  // If RLS is blocking, try with service role key (if available)
  if ((accountError || account?.is_admin === undefined) && process.env.SUPABASE_SERVICE_ROLE_KEY) {
    console.log('[Admin Auth] Trying with service role key due to:', accountError?.message || 'is_admin undefined')

    const serviceSupabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!,
      {
        auth: {
          persistSession: false,
          autoRefreshToken: false
        }
      }
    )

    const serviceResult = await serviceSupabase
      .from('accounts')
      .select('*')
      .eq('id', user.id)
      .single()

    account = serviceResult.data
    accountError = serviceResult.error
  }

  // Debug logging
  console.log('[Admin Auth] Final account query result:', {
    account,
    accountError: accountError?.message,
    userId: user.id,
    userEmail: user.email,
    isAdmin: account?.is_admin,
    hasIsAdminField: account && 'is_admin' in account
  })

  if (accountError) {
    console.error('[Admin Auth] Database error:', accountError)
    redirect('/dashboard')
  }

  if (!account?.is_admin) {
    console.log('[Admin Auth] User is not admin:', {
      userEmail: user.email,
      isAdmin: account?.is_admin,
      account
    })
    redirect('/dashboard')  // Redirect non-admins to regular dashboard
  }

  return { user, account }
}

export async function isAdmin() {
  const supabase = await createServerClientSupabase()

  const { data: { user }, error } = await supabase.auth.getUser()

  if (error || !user) {
    return false
  }

  const { data: account } = await supabase
    .from('accounts')
    .select('is_admin')
    .eq('id', user.id)
    .single()

  return account?.is_admin === true
}
