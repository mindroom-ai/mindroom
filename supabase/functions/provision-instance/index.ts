// Provision a new MindRoom instance via Dokku
import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}

interface ProvisionRequest {
  subscription_id: string
  instance_id: string
  tier: 'free' | 'starter' | 'professional' | 'enterprise'
}

interface DokkuProvisionRequest {
  app_name: string
  subdomain: string
  environment: Record<string, string>
  resources: {
    memory_mb: number
    cpu_limit: number
    disk_gb: number
  }
  config: any
}

// Tier to resource mapping
const TIER_RESOURCES = {
  free: {
    memory_mb: 512,
    cpu_limit: 0.5,
    disk_gb: 1
  },
  starter: {
    memory_mb: 1024,
    cpu_limit: 1.0,
    disk_gb: 5
  },
  professional: {
    memory_mb: 2048,
    cpu_limit: 2.0,
    disk_gb: 50
  },
  enterprise: {
    memory_mb: 4096,
    cpu_limit: 4.0,
    disk_gb: 100
  }
}

serve(async (req) => {
  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  try {
    const { subscription_id, instance_id, tier } = await req.json() as ProvisionRequest

    // Initialize Supabase admin client
    const supabaseUrl = Deno.env.get('SUPABASE_URL')!
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_KEY')!
    const supabase = createClient(supabaseUrl, supabaseServiceKey)

    console.log('Provisioning instance:', instance_id, 'for subscription:', subscription_id)

    // Get instance details
    const { data: instance, error: instanceError } = await supabase
      .from('instances')
      .select('*')
      .eq('id', instance_id)
      .single()

    if (instanceError || !instance) {
      throw new Error(`Instance not found: ${instance_id}`)
    }

    // Get subscription and account details
    const { data: subscription, error: subError } = await supabase
      .from('subscriptions')
      .select(`
        *,
        accounts(*)
      `)
      .eq('id', subscription_id)
      .single()

    if (subError || !subscription) {
      throw new Error(`Subscription not found: ${subscription_id}`)
    }

    // Generate environment variables for the instance
    const environment = {
      MINDROOM_INSTANCE_ID: instance_id,
      MINDROOM_SUBSCRIPTION_ID: subscription_id,
      MINDROOM_TIER: tier,
      MINDROOM_API_URL: supabaseUrl,
      MINDROOM_API_KEY: Deno.env.get('MINDROOM_INSTANCE_API_KEY') || '',
      MATRIX_HOMESERVER: `https://${instance.subdomain}.mindroom.app`,
      MATRIX_BOT_USERNAME: 'mindroom-bot',
      MATRIX_BOT_PASSWORD: generateSecurePassword(),
      // Add any API keys from instance config
      ...(instance.environment_vars || {})
    }

    // Prepare Dokku provisioning request
    const dokkuRequest: DokkuProvisionRequest = {
      app_name: instance.dokku_app_name,
      subdomain: instance.subdomain,
      environment,
      resources: TIER_RESOURCES[tier],
      config: instance.config || {}
    }

    // Call Dokku provisioner service
    const dokkuUrl = Deno.env.get('DOKKU_PROVISIONER_URL') || 'http://localhost:8002'
    const dokkuResponse = await fetch(`${dokkuUrl}/provision`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${Deno.env.get('DOKKU_PROVISIONER_TOKEN')}`
      },
      body: JSON.stringify(dokkuRequest)
    })

    if (!dokkuResponse.ok) {
      const error = await dokkuResponse.text()
      console.error('Dokku provisioning failed:', error)

      // Update instance status to failed
      await supabase
        .from('instances')
        .update({
          status: 'failed',
          error_message: error,
          health_details: {
            error: 'dokku_provisioning_failed',
            details: error
          }
        })
        .eq('id', instance_id)

      throw new Error(`Dokku provisioning failed: ${error}`)
    }

    const dokkuResult = await dokkuResponse.json()
    console.log('Dokku provisioning result:', dokkuResult)

    // Update instance with URLs and status
    const { error: updateError } = await supabase
      .from('instances')
      .update({
        status: 'running',
        backend_url: dokkuResult.backend_url || `https://${instance.subdomain}-api.mindroom.app`,
        frontend_url: dokkuResult.frontend_url || `https://${instance.subdomain}.mindroom.app`,
        matrix_server_url: dokkuResult.matrix_url || `https://${instance.subdomain}-matrix.mindroom.app`,
        matrix_admin_token: dokkuResult.matrix_admin_token,
        provisioned_at: new Date().toISOString(),
        last_started_at: new Date().toISOString(),
        health_status: 'healthy',
        health_details: {
          provisioned: true,
          dokku_app: dokkuResult.app_name,
          urls: {
            backend: dokkuResult.backend_url,
            frontend: dokkuResult.frontend_url,
            matrix: dokkuResult.matrix_url
          }
        }
      })
      .eq('id', instance_id)

    if (updateError) {
      console.error('Failed to update instance:', updateError)
      throw updateError
    }

    // Log provisioning success
    await supabase
      .from('audit_logs')
      .insert({
        account_id: subscription.account_id,
        instance_id: instance_id,
        action: 'instance_provisioned',
        action_category: 'instance',
        details: {
          app_name: instance.dokku_app_name,
          subdomain: instance.subdomain,
          tier: tier,
          resources: TIER_RESOURCES[tier]
        }
      })

    // Send welcome email (via another service or Resend)
    await sendWelcomeEmail(subscription.accounts.email, {
      instance_url: `https://${instance.subdomain}.mindroom.app`,
      api_docs: 'https://docs.mindroom.app',
      support_email: 'support@mindroom.app'
    })

    return new Response(
      JSON.stringify({
        success: true,
        instance_id: instance_id,
        urls: {
          backend: dokkuResult.backend_url,
          frontend: dokkuResult.frontend_url,
          matrix: dokkuResult.matrix_url
        }
      }),
      {
        status: 200,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      }
    )
  } catch (error) {
    console.error('Provisioning error:', error)

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

// Helper function to generate secure passwords
function generateSecurePassword(length = 32): string {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*'
  let password = ''
  const array = new Uint8Array(length)
  crypto.getRandomValues(array)

  for (let i = 0; i < length; i++) {
    password += chars[array[i] % chars.length]
  }

  return password
}

// Helper function to send welcome email
async function sendWelcomeEmail(email: string, data: any) {
  const resendApiKey = Deno.env.get('RESEND_API_KEY')

  if (!resendApiKey) {
    console.warn('RESEND_API_KEY not set, skipping welcome email')
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
        to: [email],
        subject: 'Welcome to MindRoom! Your Instance is Ready',
        html: `
          <h2>Welcome to MindRoom!</h2>
          <p>Your MindRoom instance has been successfully provisioned and is ready to use.</p>

          <h3>Your Instance Details:</h3>
          <ul>
            <li><strong>Dashboard URL:</strong> <a href="${data.instance_url}">${data.instance_url}</a></li>
            <li><strong>API Documentation:</strong> <a href="${data.api_docs}">${data.api_docs}</a></li>
          </ul>

          <h3>Getting Started:</h3>
          <ol>
            <li>Visit your dashboard and complete the initial setup</li>
            <li>Configure your first AI agent</li>
            <li>Connect to your preferred chat platforms (Slack, Discord, etc.)</li>
            <li>Start chatting with your AI agents!</li>
          </ol>

          <h3>Need Help?</h3>
          <p>
            Check out our <a href="${data.api_docs}">documentation</a> or
            contact us at <a href="mailto:${data.support_email}">${data.support_email}</a>
          </p>

          <p>Welcome aboard!</p>
          <p>The MindRoom Team</p>
        `
      })
    })

    if (!response.ok) {
      console.error('Failed to send welcome email:', await response.text())
    }
  } catch (error) {
    console.error('Error sending welcome email:', error)
  }
}
