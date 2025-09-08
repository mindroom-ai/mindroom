import { NextRequest, NextResponse } from 'next/server'
import Stripe from 'stripe'
import { createServerClientSupabase } from '@/lib/supabase/server'

// Initialize Stripe lazily to avoid build-time errors
function getStripe() {
  if (!process.env.STRIPE_SECRET_KEY) {
    throw new Error('Stripe secret key not configured');
  }
  return new Stripe(process.env.STRIPE_SECRET_KEY, {
    apiVersion: '2024-12-18.acacia',
  });
}

export async function POST(request: NextRequest) {
  const stripe = getStripe();

  try {
    const { priceId, tier } = await request.json()

    if (!priceId || !tier) {
      return NextResponse.json(
        { error: 'Missing required parameters' },
        { status: 400 }
      )
    }

    const supabase = await createServerClientSupabase()

    // Get the user (if logged in)
    const { data: { user } } = await supabase.auth.getUser()

    // Get or create customer
    let customerId: string | undefined

    if (user) {
      // Check if user already has an account
      const { data: account } = await supabase
        .from('accounts')
        .select('stripe_customer_id')
        .eq('email', user.email)
        .single()

      if (account?.stripe_customer_id) {
        customerId = account.stripe_customer_id
      } else {
        // Create a new Stripe customer
        const customer = await stripe.customers.create({
          email: user.email,
          metadata: {
            supabase_user_id: user.id,
          },
        })
        customerId = customer.id

        // Save the customer ID to the database
        await supabase
          .from('accounts')
          .upsert({
            id: user.id,
            email: user.email,
            stripe_customer_id: customer.id,
          })
      }
    }

    // Create checkout session
    const session = await stripe.checkout.sessions.create({
      customer: customerId,
      customer_email: !customerId ? undefined : undefined, // Only set if no customer
      line_items: [
        {
          price: priceId,
          quantity: 1,
        },
      ],
      mode: 'subscription',
      success_url: `${process.env.APP_URL}/dashboard?success=true&session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${process.env.APP_URL}/pricing?cancelled=true`,
      allow_promotion_codes: true,
      billing_address_collection: 'required',
      payment_method_collection: 'if_required',
      customer_creation: !customerId ? 'always' : undefined,
      subscription_data: {
        trial_period_days: 14, // 14-day free trial
        metadata: {
          tier,
          supabase_user_id: user?.id || '',
        },
      },
      metadata: {
        tier,
        supabase_user_id: user?.id || '',
      },
    })

    return NextResponse.json({ url: session.url })
  } catch (error) {
    console.error('Error creating checkout session:', error)
    return NextResponse.json(
      { error: 'Failed to create checkout session' },
      { status: 500 }
    )
  }
}
