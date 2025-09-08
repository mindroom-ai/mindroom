import { createServerClientSupabase } from '@/lib/supabase/server'
import { NextResponse } from 'next/server'

export async function GET() {
  try {
    const supabase = await createServerClientSupabase()

    // Get current user
    const { data: { user }, error: userError } = await supabase.auth.getUser()

    if (userError || !user) {
      return NextResponse.json({
        error: 'Not authenticated',
        userError: userError?.message,
        authenticated: false
      }, { status: 401 })
    }

    // Query account data
    const { data: account, error: accountError } = await supabase
      .from('accounts')
      .select('*')
      .eq('id', user.id)
      .single()

    // Also try a direct query for is_admin specifically
    const { data: adminCheck, error: adminError } = await supabase
      .from('accounts')
      .select('is_admin')
      .eq('id', user.id)
      .single()

    return NextResponse.json({
      success: true,
      user: {
        id: user.id,
        email: user.email
      },
      account: account || null,
      accountError: accountError?.message || null,
      adminCheck: adminCheck || null,
      adminError: adminError?.message || null,
      hasAdminField: account && 'is_admin' in account,
      isAdmin: account?.is_admin || false,
      debugInfo: {
        supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL || 'NOT SET',
        hasAnonKey: !!process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
        nodeEnv: process.env.NODE_ENV
      }
    })
  } catch (error) {
    console.error('[Debug Admin Check] Error:', error)
    return NextResponse.json({
      error: 'Internal server error',
      message: error instanceof Error ? error.message : 'Unknown error'
    }, { status: 500 })
  }
}
