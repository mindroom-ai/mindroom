import { NextResponse } from 'next/server'
import { createServerClientSupabase } from '@/lib/supabase/server'

export async function POST(request: Request) {
  try {
    const supabase = await createServerClientSupabase()

    // Get the user
    const { data: { user } } = await supabase.auth.getUser()

    if (!user) {
      return NextResponse.json(
        { error: 'Unauthorized' },
        { status: 401 }
      )
    }

    const { instanceId } = await request.json()

    // Verify the user owns this instance
    const { data: instance } = await supabase
      .from('instances')
      .select('*, subscriptions!inner(account_id)')
      .eq('id', instanceId)
      .eq('subscriptions.account_id', user.id)
      .single()

    if (!instance) {
      return NextResponse.json(
        { error: 'Instance not found' },
        { status: 404 }
      )
    }

    // Update instance status to provisioning
    const { error } = await supabase
      .from('instances')
      .update({
        status: 'provisioning',
        updated_at: new Date().toISOString()
      })
      .eq('id', instanceId)

    if (error) {
      throw error
    }

    // In a real implementation, this would trigger the actual instance restart
    // For now, we'll simulate it by setting status back to running after a delay
    setTimeout(async () => {
      await supabase
        .from('instances')
        .update({
          status: 'running',
          updated_at: new Date().toISOString()
        })
        .eq('id', instanceId)
    }, 5000) // Simulate 5 second restart

    return NextResponse.json({ success: true })
  } catch (error) {
    console.error('Error restarting instance:', error)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}
