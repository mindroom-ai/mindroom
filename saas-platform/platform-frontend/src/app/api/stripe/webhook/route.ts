import { NextRequest, NextResponse } from 'next/server'
import Stripe from 'stripe'
import { createClient } from '@supabase/supabase-js'

// Initialize Stripe lazily to avoid build-time errors
function getStripe() {
  if (!process.env.STRIPE_SECRET_KEY) {
    throw new Error('Stripe secret key not configured');
  }
  return new Stripe(process.env.STRIPE_SECRET_KEY, {
    apiVersion: '2024-12-18.acacia',
  });
}

// Create Supabase client lazily to avoid build-time errors
function getSupabase() {
  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_KEY) {
    throw new Error('Supabase configuration missing');
  }
  return createClient(
    process.env.SUPABASE_URL,
    process.env.SUPABASE_SERVICE_KEY
  );
}

// Will be validated at runtime
const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET || ''

// Map Stripe status to our status enum
const mapStripeStatus = (status: string): string => {
  const statusMap: { [key: string]: string } = {
    'trialing': 'trialing',
    'active': 'active',
    'canceled': 'cancelled',
    'incomplete': 'incomplete',
    'incomplete_expired': 'incomplete',
    'past_due': 'past_due',
    'unpaid': 'past_due',
    'paused': 'paused',
  }
  return statusMap[status] || 'active'
}

// Map price ID to tier
const mapPriceToTier = (priceId: string): string => {
  const tierMap: { [key: string]: string } = {
    [process.env.STRIPE_PRICE_STARTER!]: 'starter',
    [process.env.STRIPE_PRICE_PROFESSIONAL!]: 'professional',
    [process.env.STRIPE_PRICE_ENTERPRISE!]: 'enterprise',
  }
  return tierMap[priceId] || 'free'
}

// Get tier limits
const getTierLimits = (tier: string) => {
  const limits: { [key: string]: any } = {
    free: {
      max_agents: 1,
      max_messages_per_day: 100,
      max_storage_gb: 1,
      max_platforms: 1,
      max_team_members: 1,
    },
    starter: {
      max_agents: 5,
      max_messages_per_day: 5000,
      max_storage_gb: 10,
      max_platforms: 3,
      max_team_members: 5,
    },
    professional: {
      max_agents: 999, // Effectively unlimited
      max_messages_per_day: 50000,
      max_storage_gb: 100,
      max_platforms: 10,
      max_team_members: 25,
    },
    enterprise: {
      max_agents: 9999, // Effectively unlimited
      max_messages_per_day: 999999,
      max_storage_gb: 1000,
      max_platforms: 999,
      max_team_members: 999,
    },
  }
  return limits[tier] || limits.free
}

export async function POST(request: NextRequest) {
  const body = await request.text()
  const sig = request.headers.get('stripe-signature')

  if (!sig) {
    return NextResponse.json(
      { error: 'Missing stripe-signature header' },
      { status: 400 }
    )
  }

  if (!webhookSecret) {
    return NextResponse.json(
      { error: 'Webhook secret not configured' },
      { status: 500 }
    )
  }

  let event: Stripe.Event

  const stripe = getStripe();

  try {
    event = stripe.webhooks.constructEvent(body, sig, webhookSecret)
  } catch (err: any) {
    console.error('Webhook signature verification failed:', err.message)
    return NextResponse.json(
      { error: `Webhook Error: ${err.message}` },
      { status: 400 }
    )
  }

  const supabase = getSupabase();

  try {
    switch (event.type) {
      case 'checkout.session.completed': {
        const session = event.data.object as Stripe.Checkout.Session

        // Get the subscription details
        const subscription = await stripe.subscriptions.retrieve(
          session.subscription as string
        )

        // Get or create the account
        const customer = await stripe.customers.retrieve(
          session.customer as string
        ) as Stripe.Customer

        // Create or update the account
        const { data: account, error: accountError } = await supabase
          .from('accounts')
          .upsert({
            email: customer.email,
            stripe_customer_id: customer.id,
            full_name: customer.name,
          }, {
            onConflict: 'email',
          })
          .select()
          .single()

        if (accountError) {
          console.error('Error creating account:', accountError)
          throw accountError
        }

        // Create the subscription record
        const tier = mapPriceToTier(subscription.items.data[0].price.id)
        const limits = getTierLimits(tier)

        const { error: subError } = await supabase
          .from('subscriptions')
          .insert({
            account_id: account.id,
            stripe_subscription_id: subscription.id,
            stripe_price_id: subscription.items.data[0].price.id,
            tier,
            status: mapStripeStatus(subscription.status),
            ...limits,
            trial_ends_at: subscription.trial_end ? new Date(subscription.trial_end * 1000).toISOString() : null,
            current_period_start: new Date(subscription.current_period_start * 1000).toISOString(),
            current_period_end: new Date(subscription.current_period_end * 1000).toISOString(),
          })

        if (subError) {
          console.error('Error creating subscription:', subError)
          throw subError
        }

        console.log(`✅ Subscription created for ${customer.email}`)
        break
      }

      case 'customer.subscription.updated': {
        const subscription = event.data.object as Stripe.Subscription

        // Update subscription status and details
        const tier = mapPriceToTier(subscription.items.data[0].price.id)
        const limits = getTierLimits(tier)

        const { error } = await supabase
          .from('subscriptions')
          .update({
            stripe_price_id: subscription.items.data[0].price.id,
            tier,
            status: mapStripeStatus(subscription.status),
            ...limits,
            current_period_start: new Date(subscription.current_period_start * 1000).toISOString(),
            current_period_end: new Date(subscription.current_period_end * 1000).toISOString(),
            cancelled_at: subscription.canceled_at ? new Date(subscription.canceled_at * 1000).toISOString() : null,
          })
          .eq('stripe_subscription_id', subscription.id)

        if (error) {
          console.error('Error updating subscription:', error)
          throw error
        }

        console.log(`✅ Subscription updated: ${subscription.id}`)
        break
      }

      case 'customer.subscription.deleted': {
        const subscription = event.data.object as Stripe.Subscription

        // Mark subscription as cancelled
        const { error } = await supabase
          .from('subscriptions')
          .update({
            status: 'cancelled',
            cancelled_at: new Date().toISOString(),
          })
          .eq('stripe_subscription_id', subscription.id)

        if (error) {
          console.error('Error cancelling subscription:', error)
          throw error
        }

        console.log(`✅ Subscription cancelled: ${subscription.id}`)
        break
      }

      case 'invoice.payment_succeeded': {
        const invoice = event.data.object as Stripe.Invoice

        // Reset daily usage limits on successful payment
        if (invoice.subscription) {
          const { error } = await supabase
            .from('subscriptions')
            .update({
              current_messages_today: 0,
              last_reset_at: new Date().toISOString().split('T')[0],
            })
            .eq('stripe_subscription_id', invoice.subscription)

          if (error) {
            console.error('Error resetting usage:', error)
          }
        }
        break
      }

      case 'invoice.payment_failed': {
        const invoice = event.data.object as Stripe.Invoice

        // Update subscription status to past_due
        if (invoice.subscription) {
          const { error } = await supabase
            .from('subscriptions')
            .update({
              status: 'past_due',
            })
            .eq('stripe_subscription_id', invoice.subscription)

          if (error) {
            console.error('Error updating subscription status:', error)
          }
        }
        break
      }

      default:
        console.log(`Unhandled event type: ${event.type}`)
    }

    return NextResponse.json({ received: true })
  } catch (error) {
    console.error('Webhook handler error:', error)
    return NextResponse.json(
      { error: 'Webhook handler failed' },
      { status: 500 }
    )
  }
}
