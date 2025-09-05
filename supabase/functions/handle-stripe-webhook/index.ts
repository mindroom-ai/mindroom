// Handle Stripe webhook events for subscription management
import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"
import Stripe from "https://esm.sh/stripe@13"

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, stripe-signature',
}

interface StripeCustomer {
  id: string
  email: string
  name?: string
  metadata?: Record<string, string>
}

interface StripeSubscription {
  id: string
  customer: string | StripeCustomer
  status: string
  current_period_start: number
  current_period_end: number
  cancel_at_period_end: boolean
  canceled_at: number | null
  trial_end: number | null
  items: {
    data: Array<{
      price: {
        id: string
        product: string
        recurring?: {
          interval: string
        }
      }
    }>
  }
  metadata?: Record<string, string>
}

// Price ID to tier mapping
const PRICE_TO_TIER: Record<string, string> = {
  'price_free': 'free',
  'price_starter_monthly': 'starter',
  'price_starter_yearly': 'starter',
  'price_professional_monthly': 'professional',
  'price_professional_yearly': 'professional',
  'price_enterprise_monthly': 'enterprise',
  'price_enterprise_yearly': 'enterprise',
}

serve(async (req) => {
  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  try {
    const signature = req.headers.get('stripe-signature')
    if (!signature) {
      return new Response(
        JSON.stringify({ error: 'Missing stripe-signature header' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    const body = await req.text()

    // Initialize Stripe
    const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY')!, {
      apiVersion: '2023-10-16',
      httpClient: Stripe.createFetchHttpClient(),
    })

    // Verify webhook signature
    const webhookSecret = Deno.env.get('STRIPE_WEBHOOK_SECRET')!
    let event: Stripe.Event

    try {
      event = stripe.webhooks.constructEvent(body, signature, webhookSecret)
    } catch (err) {
      console.error('Webhook signature verification failed:', err)
      return new Response(
        JSON.stringify({ error: 'Invalid signature' }),
        { status: 401, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    // Initialize Supabase admin client
    const supabaseUrl = Deno.env.get('SUPABASE_URL')!
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_KEY')!
    const supabase = createClient(supabaseUrl, supabaseServiceKey)

    console.log('Processing webhook event:', event.type)

    // Handle different event types
    switch (event.type) {
      case 'customer.created': {
        const customer = event.data.object as StripeCustomer

        // Create or update account
        const { error } = await supabase
          .from('accounts')
          .upsert({
            email: customer.email!,
            full_name: customer.name,
            stripe_customer_id: customer.id,
          }, {
            onConflict: 'stripe_customer_id'
          })

        if (error) {
          console.error('Error upserting account:', error)
          throw error
        }

        console.log('Account created/updated for customer:', customer.id)
        break
      }

      case 'customer.subscription.created': {
        const subscription = event.data.object as StripeSubscription
        const customerId = typeof subscription.customer === 'string'
          ? subscription.customer
          : subscription.customer.id

        // Get account ID
        const { data: account } = await supabase
          .from('accounts')
          .select('id')
          .eq('stripe_customer_id', customerId)
          .single()

        if (!account) {
          throw new Error(`Account not found for customer: ${customerId}`)
        }

        // Determine tier from price ID
        const priceId = subscription.items.data[0]?.price.id
        const tier = PRICE_TO_TIER[priceId] || 'free'

        // Create subscription record
        const { data: sub, error: subError } = await supabase
          .from('subscriptions')
          .insert({
            account_id: account.id,
            stripe_subscription_id: subscription.id,
            stripe_price_id: priceId,
            tier: tier,
            status: subscription.status as any,
            trial_ends_at: subscription.trial_end
              ? new Date(subscription.trial_end * 1000).toISOString()
              : null,
            current_period_start: new Date(subscription.current_period_start * 1000).toISOString(),
            current_period_end: new Date(subscription.current_period_end * 1000).toISOString(),
          })
          .select('id')
          .single()

        if (subError) {
          console.error('Error creating subscription:', subError)
          throw subError
        }

        console.log('Subscription created:', sub.id)

        // Provision instance for new subscription
        const { data: instanceData } = await supabase
          .rpc('provision_instance', {
            p_subscription_id: sub.id,
            p_config: subscription.metadata?.config ? JSON.parse(subscription.metadata.config) : null
          })

        console.log('Instance provisioning initiated:', instanceData)

        // Trigger instance provisioning via Edge Function
        const provisionUrl = `${supabaseUrl}/functions/v1/provision-instance`
        const provisionResponse = await fetch(provisionUrl, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${supabaseServiceKey}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            subscription_id: sub.id,
            instance_id: instanceData,
            tier: tier
          })
        })

        if (!provisionResponse.ok) {
          console.error('Failed to trigger instance provisioning:', await provisionResponse.text())
        }

        break
      }

      case 'customer.subscription.updated': {
        const subscription = event.data.object as StripeSubscription
        const priceId = subscription.items.data[0]?.price.id
        const tier = PRICE_TO_TIER[priceId] || 'free'

        // Update subscription
        const { error } = await supabase
          .from('subscriptions')
          .update({
            status: subscription.status as any,
            tier: tier,
            stripe_price_id: priceId,
            current_period_start: new Date(subscription.current_period_start * 1000).toISOString(),
            current_period_end: new Date(subscription.current_period_end * 1000).toISOString(),
          })
          .eq('stripe_subscription_id', subscription.id)

        if (error) {
          console.error('Error updating subscription:', error)
          throw error
        }

        console.log('Subscription updated:', subscription.id)
        break
      }

      case 'customer.subscription.deleted': {
        const subscription = event.data.object as StripeSubscription

        // Get subscription and instances
        const { data: subData } = await supabase
          .from('subscriptions')
          .select('id, instances(id)')
          .eq('stripe_subscription_id', subscription.id)
          .single()

        if (subData) {
          // Update subscription status
          await supabase
            .from('subscriptions')
            .update({
              status: 'cancelled',
              cancelled_at: new Date().toISOString()
            })
            .eq('id', subData.id)

          // Trigger instance deprovisioning for all instances
          if (subData.instances && Array.isArray(subData.instances)) {
            for (const instance of subData.instances) {
              const deprovisionUrl = `${supabaseUrl}/functions/v1/deprovision-instance`
              await fetch(deprovisionUrl, {
                method: 'POST',
                headers: {
                  'Authorization': `Bearer ${supabaseServiceKey}`,
                  'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                  instance_id: instance.id,
                  reason: 'subscription_cancelled'
                })
              })
            }
          }
        }

        console.log('Subscription cancelled:', subscription.id)
        break
      }

      case 'invoice.payment_succeeded': {
        const invoice = event.data.object as any

        // Reset usage limits for new billing period
        if (invoice.subscription) {
          await supabase
            .from('subscriptions')
            .update({
              current_messages_today: 0,
              last_reset_at: new Date().toISOString().split('T')[0]
            })
            .eq('stripe_subscription_id', invoice.subscription)
        }

        console.log('Payment succeeded for invoice:', invoice.id)
        break
      }

      case 'invoice.payment_failed': {
        const invoice = event.data.object as any

        // Update subscription status
        if (invoice.subscription) {
          await supabase
            .from('subscriptions')
            .update({
              status: 'past_due'
            })
            .eq('stripe_subscription_id', invoice.subscription)

          // Optionally pause instances
          const { data: instances } = await supabase
            .from('instances')
            .select('id')
            .eq('subscription_id', invoice.subscription)

          if (instances) {
            for (const instance of instances) {
              await supabase
                .from('instances')
                .update({
                  status: 'stopped',
                  health_details: { reason: 'payment_failed' }
                })
                .eq('id', instance.id)
            }
          }
        }

        console.log('Payment failed for invoice:', invoice.id)
        break
      }

      default:
        console.log('Unhandled event type:', event.type)
    }

    // Log the event
    await supabase
      .from('audit_logs')
      .insert({
        action: `stripe_webhook_${event.type}`,
        action_category: 'billing',
        details: {
          event_id: event.id,
          type: event.type,
          data: event.data.object
        }
      })

    return new Response(
      JSON.stringify({ received: true, type: event.type }),
      {
        status: 200,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      }
    )
  } catch (error) {
    console.error('Webhook processing error:', error)

    return new Response(
      JSON.stringify({ error: error.message }),
      {
        status: 500,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      }
    )
  }
})
