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

    // Use service client for database operations (bypasses RLS)
    const supabase = createServiceClient()

    // Check if account already exists
    const { data: existingAccount } = await supabase
      .from('accounts')
      .select('id')
      .eq('email', user.email)
      .single()

    if (existingAccount) {
      // Account already exists, check for subscription
      const { data: existingSubscription } = await supabase
        .from('subscriptions')
        .select('id, tier')
        .eq('account_id', existingAccount.id)
        .single()

      if (existingSubscription) {
        return NextResponse.json({
          message: 'Account already set up',
          hasSubscription: true,
          tier: existingSubscription.tier
        })
      }
    }

    // Create account if it doesn't exist
    let accountId = existingAccount?.id
    if (!accountId) {
      const { data: newAccount, error: accountError } = await supabase
        .from('accounts')
        .insert({
          email: user.email,
          full_name: user.user_metadata?.full_name || user.user_metadata?.name || null,
        })
        .select('id')
        .single()

      if (accountError) {
        // Check if it's a unique constraint violation (account already exists)
        if (accountError.code === '23505') {
          // Try to fetch the existing account again
          const { data: retryAccount } = await supabase
            .from('accounts')
            .select('id')
            .eq('email', user.email)
            .single()

          if (retryAccount) {
            accountId = retryAccount.id
          } else {
            console.error('Error creating account:', accountError)
            return NextResponse.json(
              { error: 'Failed to create account' },
              { status: 500 }
            )
          }
        } else {
          console.error('Error creating account:', accountError)
          return NextResponse.json(
            { error: 'Failed to create account' },
            { status: 500 }
          )
        }
      } else {
        accountId = newAccount.id
      }
    }

    // Create free tier subscription
    const { data: subscription, error: subError } = await supabase
      .from('subscriptions')
      .insert({
        account_id: accountId,
        tier: 'free',
        status: 'active',
        max_agents: 1,
        max_messages_per_day: 100,
      })
      .select('id')
      .single()

    if (subError) {
      console.error('Error creating subscription:', subError)
      return NextResponse.json(
        { error: 'Failed to create subscription' },
        { status: 500 }
      )
    }

    // Trigger instance provisioning via the provisioner service
    const provisionerUrl = process.env.PROVISIONER_URL || 'http://instance-provisioner:8002'

    const provisionResponse = await fetch(`${provisionerUrl}/api/v1/provision`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${process.env.PROVISIONER_API_KEY || ''}`,
      },
      body: JSON.stringify({
        subscription_id: subscription.id,
        account_id: accountId,
        tier: 'free',
        custom_domain: null
      })
    })

    if (!provisionResponse.ok) {
      console.error('Failed to provision instance:', await provisionResponse.text())
      // Don't fail the whole process, instance can be provisioned later
    } else {
      const provisionData = await provisionResponse.json()

      // Create instance record with data from provisioner
      await supabase
        .from('instances')
        .insert({
          subscription_id: subscription.id,
          instance_id: provisionData.customer_id,
          subdomain: provisionData.customer_id,
          status: 'running',
          frontend_url: provisionData.frontend_url,
          backend_url: provisionData.api_url,
          memory_limit_mb: 512,
          cpu_limit: 0.5,
          auth_token: provisionData.auth_token,
        })
    }

    return NextResponse.json({
      message: 'Free tier account created successfully',
      subscriptionId: subscription.id,
      tier: 'free'
    })

  } catch (error) {
    console.error('Error in auth setup:', error)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}
