import { NextResponse } from 'next/server'
import { createServerClientSupabase } from '@/lib/supabase/server'
import { createServiceClient } from '@/lib/supabase/service'

export async function POST(request: Request) {
  try {
    // Use regular client for auth check
    const authClient = await createServerClientSupabase()

    // Get the user
    const { data: { user } } = await authClient.auth.getUser()

    if (!user) {
      return NextResponse.json(
        { error: 'Unauthorized' },
        { status: 401 }
      )
    }

    const { instanceId } = await request.json()

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

    // Verify the user owns this instance
    const { data: instance } = await supabase
      .from('instances')
      .select('*, subscriptions!inner(account_id)')
      .eq('id', instanceId)
      .eq('subscriptions.account_id', account.id)
      .single()

    if (!instance) {
      return NextResponse.json(
        { error: 'Instance not found' },
        { status: 404 }
      )
    }

    // Call the provisioner to start the instance
    const provisionerUrl = process.env.PROVISIONER_URL || 'http://instance-provisioner:8002'

    const startResponse = await fetch(`${provisionerUrl}/api/v1/start/${instance.instance_id || instance.subdomain}`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${process.env.PROVISIONER_API_KEY || ''}`,
      }
    })

    if (!startResponse.ok) {
      const error = await startResponse.text()
      console.error('Failed to start instance:', error)
      return NextResponse.json(
        { error: 'Failed to start instance', details: error },
        { status: 500 }
      )
    }

    const startData = await startResponse.json()

    // Update instance status in database
    const { error: updateError } = await supabase
      .from('instances')
      .update({
        status: 'running',
        updated_at: new Date().toISOString()
      } as any)
      .eq('id', instanceId)

    if (updateError) {
      console.error('Failed to update instance status:', updateError)
    }

    return NextResponse.json({
      success: startData.success,
      message: startData.message
    })
  } catch (error) {
    console.error('Error starting instance:', error)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}
