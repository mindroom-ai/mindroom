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

    // Call the provisioner to restart the instance
    const provisionerUrl = process.env.PLATFORM_BACKEND_URL
    if (!provisionerUrl) {
      throw new Error('PLATFORM_BACKEND_URL environment variable is not configured')
    }

    const restartResponse = await fetch(`${provisionerUrl}/api/v1/restart/${instance.instance_id || instance.subdomain}`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${process.env.PROVISIONER_API_KEY || ''}`,
      }
    })

    if (!restartResponse.ok) {
      const error = await restartResponse.text()
      console.error('Failed to restart instance:', error)

      // Check if it's a "deployment not found" error
      if (error.includes('not found') || error.includes('NotFound') || error.includes('No resources found')) {
        // Update database to reflect that instance doesn't exist in cluster
        await supabase
          .from('instances')
          .update({
            status: 'error',
            updated_at: new Date().toISOString()
          } as any)
          .eq('id', instanceId)

        return NextResponse.json(
          { error: 'Instance not found in cluster. It may have been removed. Please contact support to reprovision.' },
          { status: 404 }
        )
      }

      return NextResponse.json(
        { error: 'Failed to restart instance', details: error },
        { status: 500 }
      )
    }

    const restartData = await restartResponse.json()

    // Only update instance status if restart was successful
    const { error: updateError } = await supabase
      .from('instances')
      .update({
        status: 'running',
        updated_at: new Date().toISOString()
      } as any)
      .eq('id', instanceId)

    if (updateError) {
      console.error('Failed to update instance status:', updateError)
      // Still return success since the restart worked
    }

    return NextResponse.json({
      success: restartData.success,
      message: restartData.message
    })
  } catch (error) {
    console.error('Error restarting instance:', error)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}
