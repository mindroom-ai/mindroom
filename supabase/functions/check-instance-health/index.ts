// Check health status of MindRoom instances
import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}

interface HealthCheckRequest {
  instance_id?: string
  check_all?: boolean
}

interface HealthCheckResult {
  instance_id: string
  status: 'healthy' | 'degraded' | 'critical' | 'unreachable'
  checks: {
    backend: boolean
    frontend: boolean
    matrix: boolean
    database: boolean
  }
  metrics: {
    response_time_ms: number
    memory_usage_mb?: number
    cpu_usage_percent?: number
    error_rate?: number
  }
  details?: any
}

serve(async (req) => {
  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  try {
    const body = await req.text()
    const request: HealthCheckRequest = body ? JSON.parse(body) : { check_all: true }

    // Initialize Supabase admin client
    const supabaseUrl = Deno.env.get('SUPABASE_URL')!
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_KEY')!
    const supabase = createClient(supabaseUrl, supabaseServiceKey)

    let instances = []

    if (request.instance_id) {
      // Check specific instance
      const { data, error } = await supabase
        .from('instances')
        .select('*')
        .eq('id', request.instance_id)
        .eq('status', 'running')
        .single()

      if (error || !data) {
        throw new Error(`Instance not found or not running: ${request.instance_id}`)
      }
      instances = [data]
    } else if (request.check_all) {
      // Check all running instances
      const { data, error } = await supabase
        .from('instances')
        .select('*')
        .eq('status', 'running')

      if (error) throw error
      instances = data || []
    }

    const results: HealthCheckResult[] = []

    for (const instance of instances) {
      console.log(`Checking health for instance: ${instance.id}`)

      const startTime = Date.now()
      const checks = {
        backend: false,
        frontend: false,
        matrix: false,
        database: true // Always true since we're using Supabase
      }

      // Check backend health
      if (instance.backend_url) {
        try {
          const backendResponse = await fetch(`${instance.backend_url}/health`, {
            method: 'GET',
            signal: AbortSignal.timeout(5000)
          })
          checks.backend = backendResponse.ok
        } catch (error) {
          console.error(`Backend health check failed for ${instance.id}:`, error)
        }
      }

      // Check frontend health
      if (instance.frontend_url) {
        try {
          const frontendResponse = await fetch(instance.frontend_url, {
            method: 'GET',
            signal: AbortSignal.timeout(5000)
          })
          checks.frontend = frontendResponse.ok
        } catch (error) {
          console.error(`Frontend health check failed for ${instance.id}:`, error)
        }
      }

      // Check Matrix server health
      if (instance.matrix_server_url) {
        try {
          const matrixResponse = await fetch(`${instance.matrix_server_url}/_matrix/client/versions`, {
            method: 'GET',
            signal: AbortSignal.timeout(5000)
          })
          checks.matrix = matrixResponse.ok
        } catch (error) {
          console.error(`Matrix health check failed for ${instance.id}:`, error)
        }
      }

      const responseTime = Date.now() - startTime

      // Determine overall health status
      const healthyChecks = Object.values(checks).filter(v => v).length
      const totalChecks = Object.keys(checks).length

      let status: HealthCheckResult['status']
      if (healthyChecks === totalChecks) {
        status = 'healthy'
      } else if (healthyChecks >= totalChecks * 0.75) {
        status = 'degraded'
      } else if (healthyChecks > 0) {
        status = 'critical'
      } else {
        status = 'unreachable'
      }

      const result: HealthCheckResult = {
        instance_id: instance.id,
        status,
        checks,
        metrics: {
          response_time_ms: responseTime
        }
      }

      results.push(result)

      // Update instance health in database
      await supabase
        .from('instances')
        .update({
          last_health_check: new Date().toISOString(),
          health_status: status,
          health_details: {
            checks,
            response_time_ms: responseTime,
            checked_at: new Date().toISOString()
          }
        })
        .eq('id', instance.id)

      // Log critical health events
      if (status === 'critical' || status === 'unreachable') {
        const { data: sub } = await supabase
          .from('subscriptions')
          .select('account_id')
          .eq('id', instance.subscription_id)
          .single()

        await supabase
          .from('audit_logs')
          .insert({
            account_id: sub?.account_id,
            instance_id: instance.id,
            action: 'instance_health_alert',
            action_category: 'instance',
            details: {
              status,
              checks,
              subdomain: instance.subdomain
            },
            success: false
          })

        // Send alert if instance is unreachable
        if (status === 'unreachable') {
          await sendHealthAlert(instance.id, instance.subdomain, status)
        }
      }

      // Calculate and update uptime
      if (instance.last_started_at) {
        const uptimeHours = (Date.now() - new Date(instance.last_started_at).getTime()) / (1000 * 60 * 60)
        const downtimeIncidents = status === 'unreachable' ? 1 : 0
        const uptimePercentage = Math.max(0, Math.min(100,
          ((uptimeHours - downtimeIncidents * 0.25) / uptimeHours) * 100
        ))

        await supabase
          .from('instances')
          .update({
            uptime_percentage: uptimePercentage.toFixed(2)
          })
          .eq('id', instance.id)
      }
    }

    return new Response(
      JSON.stringify({
        checked: results.length,
        results,
        timestamp: new Date().toISOString()
      }),
      {
        status: 200,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      }
    )
  } catch (error) {
    console.error('Health check error:', error)

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

// Helper function to send health alerts
async function sendHealthAlert(instanceId: string, subdomain: string, status: string) {
  const supabaseUrl = Deno.env.get('SUPABASE_URL')!
  const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_KEY')!
  const supabase = createClient(supabaseUrl, supabaseServiceKey)

  // Get account details
  const { data } = await supabase
    .from('instances')
    .select(`
      subscriptions(
        accounts(email),
        tier
      )
    `)
    .eq('id', instanceId)
    .single()

  if (!data?.subscriptions?.accounts?.email) return

  // Only send alerts for paid tiers
  if (data.subscriptions.tier === 'free') return

  const resendApiKey = Deno.env.get('RESEND_API_KEY')

  if (!resendApiKey) {
    console.warn('RESEND_API_KEY not set, skipping health alert')
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
        from: 'MindRoom Alerts <alerts@mindroom.app>',
        to: [data.subscriptions.accounts.email],
        subject: `⚠️ MindRoom Instance Health Alert: ${subdomain}`,
        html: `
          <h2>Instance Health Alert</h2>
          <p>Your MindRoom instance <strong>${subdomain}.mindroom.app</strong> is currently <strong>${status}</strong>.</p>

          <h3>What This Means:</h3>
          <p>We've detected that your instance is not responding to health checks. This could be due to:</p>
          <ul>
            <li>High load or resource constraints</li>
            <li>Network connectivity issues</li>
            <li>Application errors</li>
          </ul>

          <h3>What We're Doing:</h3>
          <p>Our team has been notified and is investigating the issue. We'll work to restore your instance as quickly as possible.</p>

          <h3>What You Can Do:</h3>
          <ul>
            <li>Check your instance dashboard for more details</li>
            <li>Review recent configuration changes</li>
            <li>Contact support if the issue persists</li>
          </ul>

          <p>
            <a href="https://app.mindroom.app/instances/${instanceId}"
               style="background: #4F46E5; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">
              View Instance Status
            </a>
          </p>

          <p>If you need immediate assistance, please contact our support team.</p>

          <p>The MindRoom Team</p>
        `
      })
    })

    if (!response.ok) {
      console.error('Failed to send health alert:', await response.text())
    }
  } catch (error) {
    console.error('Error sending health alert:', error)
  }
}
