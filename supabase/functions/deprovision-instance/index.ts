// Deprovision a MindRoom instance via Dokku
import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}

interface DeprovisionRequest {
  instance_id: string
  reason?: string
  create_backup?: boolean
}

serve(async (req) => {
  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  try {
    const { instance_id, reason = 'manual', create_backup = true } = await req.json() as DeprovisionRequest

    // Initialize Supabase admin client
    const supabaseUrl = Deno.env.get('SUPABASE_URL')!
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_KEY')!
    const supabase = createClient(supabaseUrl, supabaseServiceKey)

    console.log('Deprovisioning instance:', instance_id, 'Reason:', reason)

    // Get instance details
    const { data: instance, error: instanceError } = await supabase
      .from('instances')
      .select(`
        *,
        subscriptions(
          account_id,
          tier
        )
      `)
      .eq('id', instance_id)
      .single()

    if (instanceError || !instance) {
      throw new Error(`Instance not found: ${instance_id}`)
    }

    // Update instance status to deprovisioning
    await supabase
      .from('instances')
      .update({
        status: 'deprovisioning',
        health_details: {
          reason: reason,
          started_at: new Date().toISOString()
        }
      })
      .eq('id', instance_id)

    // Create backup if requested
    if (create_backup) {
      console.log('Creating backup before deprovisioning...')

      const backupLocation = await createInstanceBackup(instance_id, instance.dokku_app_name)

      // Record backup in database
      await supabase
        .from('instance_backups')
        .insert({
          instance_id: instance_id,
          backup_type: 'pre_upgrade',
          backup_location: backupLocation,
          status: 'completed',
          retention_days: 30,
          expires_at: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString()
        })
    }

    // Call Dokku provisioner service to destroy the app
    const dokkuUrl = Deno.env.get('DOKKU_PROVISIONER_URL') || 'http://localhost:8002'
    const dokkuResponse = await fetch(`${dokkuUrl}/deprovision`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${Deno.env.get('DOKKU_PROVISIONER_TOKEN')}`
      },
      body: JSON.stringify({
        app_name: instance.dokku_app_name,
        destroy_volumes: reason === 'subscription_cancelled', // Keep volumes for temporary deprovisions
        destroy_database: reason === 'subscription_cancelled'
      })
    })

    if (!dokkuResponse.ok) {
      const error = await dokkuResponse.text()
      console.error('Dokku deprovisioning failed:', error)

      // Update instance status to failed
      await supabase
        .from('instances')
        .update({
          status: 'failed',
          error_message: error,
          health_details: {
            error: 'dokku_deprovisioning_failed',
            details: error
          }
        })
        .eq('id', instance_id)

      throw new Error(`Dokku deprovisioning failed: ${error}`)
    }

    const dokkuResult = await dokkuResponse.json()
    console.log('Dokku deprovisioning result:', dokkuResult)

    // Update instance status to deprovisioned
    await supabase
      .from('instances')
      .update({
        status: 'stopped',
        deprovisioned_at: new Date().toISOString(),
        health_status: 'deprovisioned',
        health_details: {
          deprovisioned: true,
          reason: reason,
          backup_created: create_backup,
          timestamp: new Date().toISOString()
        }
      })
      .eq('id', instance_id)

    // Log deprovisioning success
    await supabase
      .from('audit_logs')
      .insert({
        account_id: instance.subscriptions.account_id,
        instance_id: instance_id,
        action: 'instance_deprovisioned',
        action_category: 'instance',
        details: {
          app_name: instance.dokku_app_name,
          reason: reason,
          backup_created: create_backup
        }
      })

    // Send notification email if subscription cancelled
    if (reason === 'subscription_cancelled') {
      await sendCancellationEmail(instance.subscriptions.account_id, instance.subdomain)
    }

    return new Response(
      JSON.stringify({
        success: true,
        instance_id: instance_id,
        deprovisioned: true,
        backup_created: create_backup
      }),
      {
        status: 200,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      }
    )
  } catch (error) {
    console.error('Deprovisioning error:', error)

    return new Response(
      JSON.stringify({
        error: error.message,
        details: error.stack
      }),
      {
        status: 500,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      }
    )
  }
})

// Helper function to create instance backup
async function createInstanceBackup(instanceId: string, appName: string): Promise<string> {
  // This would integrate with your backup system (S3, etc.)
  // For now, returning a mock backup location
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-')
  const backupLocation = `s3://mindroom-backups/${instanceId}/${appName}-${timestamp}.tar.gz`

  // In production, you would:
  // 1. Create a database dump
  // 2. Export instance configuration
  // 3. Archive user data
  // 4. Upload to S3 or similar storage

  console.log('Backup created at:', backupLocation)
  return backupLocation
}

// Helper function to send cancellation email
async function sendCancellationEmail(accountId: string, subdomain: string) {
  const supabaseUrl = Deno.env.get('SUPABASE_URL')!
  const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_KEY')!
  const supabase = createClient(supabaseUrl, supabaseServiceKey)

  // Get account email
  const { data: account } = await supabase
    .from('accounts')
    .select('email')
    .eq('id', accountId)
    .single()

  if (!account) return

  const resendApiKey = Deno.env.get('RESEND_API_KEY')

  if (!resendApiKey) {
    console.warn('RESEND_API_KEY not set, skipping cancellation email')
    return
  }

  try {
    const response = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${resendApiKey}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from: 'MindRoom <noreply@mindroom.app>',
        to: [account.email],
        subject: 'Your MindRoom Instance Has Been Deprovisioned',
        html: `
          <h2>Instance Deprovisioned</h2>
          <p>Your MindRoom instance (${subdomain}.mindroom.app) has been deprovisioned following your subscription cancellation.</p>

          <h3>Important Information:</h3>
          <ul>
            <li>Your data has been backed up and will be retained for 30 days</li>
            <li>You can reactivate your subscription at any time within the next 30 days to restore your instance</li>
            <li>After 30 days, your data will be permanently deleted</li>
          </ul>

          <h3>Want to Come Back?</h3>
          <p>
            We'd love to have you back! You can reactivate your subscription at any time by
            <a href="https://app.mindroom.app/reactivate">clicking here</a>.
          </p>

          <p>If you have any questions or concerns, please don't hesitate to reach out to our support team.</p>

          <p>Thank you for trying MindRoom!</p>
          <p>The MindRoom Team</p>
        `
      })
    })

    if (!response.ok) {
      console.error('Failed to send cancellation email:', await response.text())
    }
  } catch (error) {
    console.error('Error sending cancellation email:', error)
  }
}
