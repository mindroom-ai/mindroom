import { redirect } from 'next/navigation'
import { createServerClientSupabase } from '@/lib/supabase/server'

export async function requireAdmin() {
  const supabase = await createServerClientSupabase()

  const { data: { user }, error } = await supabase.auth.getUser()

  if (error || !user) {
    redirect('/auth/login')
  }

  // Check if user is admin
  const { data: account, error: accountError } = await supabase
    .from('accounts')
    .select('is_admin')
    .eq('id', user.id)
    .single()

  if (accountError || !account?.is_admin) {
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
