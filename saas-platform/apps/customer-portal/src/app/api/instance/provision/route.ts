import { createServerClientSupabase } from '@/lib/supabase/server'
import { createServiceClient } from '@/lib/supabase/service'
import { NextResponse } from 'next/server'

export async function POST() {
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

    // Get account
    const { data: account } = await supabase
      .from('accounts')
      .select('id')
      .eq('email', user.email)
      .single()

    if (!account) {
      return NextResponse.json(
        { error: 'Account not found' },
        { status: 404 }
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
        { error: 'No subscription found' },
        { status: 404 }
      )
    }

    // Check if instance already exists
    const { data: existingInstance } = await supabase
      .from('instances')
      .select('id')
      .eq('subscription_id', subscription.id)
      .single()

    if (existingInstance) {
      return NextResponse.json(
        { error: 'Instance already exists' },
        { status: 400 }
      )
    }

    // Call the provisioner
    const provisionerUrl = process.env.PROVISIONER_URL || 'http://instance-provisioner:8002'

    const provisionResponse = await fetch(`${provisionerUrl}/api/v1/provision`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${process.env.PROVISIONER_API_KEY || ''}`,
      },
      body: JSON.stringify({
        subscription_id: subscription.id,
        account_id: account.id,
        tier: subscription.tier || 'free',
        custom_domain: null
      })
    })

    if (!provisionResponse.ok) {
      const error = await provisionResponse.text()
      console.error('Failed to provision instance:', error)
      return NextResponse.json(
        { error: 'Failed to provision instance', details: error },
        { status: 500 }
      )
    }

    const provisionData = await provisionResponse.json()

    // Create instance record
    const { data: newInstance, error: insertError } = await supabase
      .from('instances')
      .insert({
        subscription_id: subscription.id,
        instance_id: provisionData.customer_id,
        subdomain: provisionData.customer_id,
        dokku_app_name: provisionData.customer_id, // For compatibility
        status: 'running',
        frontend_url: provisionData.frontend_url,
        backend_url: provisionData.api_url,
        matrix_server_url: provisionData.matrix_url,
        memory_limit_mb: subscription.tier === 'free' ? 512 : 1024,
        cpu_limit: subscription.tier === 'free' ? 0.5 : 1.0,
        auth_token: provisionData.auth_token,
      })
      .select()
      .single()

    if (insertError) {
      console.error('Failed to create instance record:', insertError)
      return NextResponse.json(
        { error: 'Instance provisioned but failed to save record' },
        { status: 500 }
      )
    }

    return NextResponse.json({
      message: 'Instance provisioned successfully',
      instance: newInstance
    })

  } catch (error) {
    console.error('Error in provision endpoint:', error)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}
