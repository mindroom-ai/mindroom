import { createServerClientSupabase } from '@/lib/supabase/server'
import { createServiceClient } from '@/lib/supabase/service'
import { NextResponse } from 'next/server'

export async function GET() {
  try {
    // Use regular client for auth check
    const authClient = await createServerClientSupabase()

    // Get current user
    const { data: { user }, error: authError } = await authClient.auth.getUser()
    if (authError || !user) {
      return NextResponse.json(
        { error: 'Not authenticated' },
        { status: 401 }
      )
    }

    // Use service client for database operations
    const supabase = createServiceClient()

    // Get account by email
    const { data: account } = await supabase
      .from('accounts')
      .select('id')
      .eq('email', user.email)
      .single()

    if (!account) {
      return NextResponse.json(
        { subscription: null, instance: null }
      )
    }

    // Get subscription
    const { data: subscription } = await supabase
      .from('subscriptions')
      .select('*')
      .eq('account_id', account.id)
      .single()

    if (!subscription) {
      return NextResponse.json(
        { subscription: null, instance: null }
      )
    }

    // Get instance
    const { data: instance } = await supabase
      .from('instances')
      .select('*')
      .eq('subscription_id', subscription.id)
      .single()

    return NextResponse.json({
      subscription,
      instance: instance || null
    })

  } catch (error) {
    console.error('Error fetching instance status:', error)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}
