import { NextResponse } from 'next/server'
import { createServerClientSupabase } from '@/lib/supabase/server'
import Stripe from 'stripe'

// Initialize Stripe lazily to avoid build-time errors
function getStripe() {
  if (!process.env.STRIPE_SECRET_KEY) {
    throw new Error('Stripe secret key not configured');
  }
  return new Stripe(process.env.STRIPE_SECRET_KEY, {
    apiVersion: '2024-12-18.acacia',
  });
}

export async function POST() {
  const stripe = getStripe();

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

    // Get the user's subscription
    const { data: subscription } = await supabase
      .from('subscriptions')
      .select('stripe_customer_id')
      .eq('account_id', user.id)
      .single()

    if (!subscription?.stripe_customer_id) {
      return NextResponse.json(
        { error: 'No Stripe customer found' },
        { status: 404 }
      )
    }

    // Create a Stripe billing portal session
    const session = await stripe.billingPortal.sessions.create({
      customer: subscription.stripe_customer_id,
      return_url: `${process.env.NEXT_PUBLIC_APP_URL}/dashboard/billing`,
    })

    return NextResponse.json({ url: session.url })
  } catch (error) {
    console.error('Error creating portal session:', error)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}
