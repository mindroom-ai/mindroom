import { createClient, SupabaseClient } from '@supabase/supabase-js';
import Stripe from 'stripe';
import { config } from '../config';
import {
  Account,
  Subscription,
  Instance,
  UsageRecord,
  WebhookEvent,
} from '../types';

// Initialize Supabase client with service key for admin access
export const supabase: SupabaseClient = createClient(
  config.supabase.url,
  config.supabase.serviceKey,
  {
    auth: {
      autoRefreshToken: false,
      persistSession: false
    }
  }
);

// Account operations
export async function getOrCreateAccount(customerId: string, email?: string): Promise<Account> {
  // First try to find existing account
  const { data: existing, error: findError } = await supabase
    .from('accounts')
    .select('*')
    .eq('stripe_customer_id', customerId)
    .single();

  if (existing && !findError) {
    return existing;
  }

  // Create new account if not found
  const { data: account, error: createError } = await supabase
    .from('accounts')
    .insert({
      stripe_customer_id: customerId,
      email: email || '',
      created_at: new Date(),
      updated_at: new Date(),
    })
    .select()
    .single();

  if (createError) {
    console.error('Error creating account:', createError);
    throw createError;
  }

  return account!;
}

export async function updateAccount(customerId: string, updates: Partial<Account>): Promise<Account> {
  const { data, error } = await supabase
    .from('accounts')
    .update({
      ...updates,
      updated_at: new Date(),
    })
    .eq('stripe_customer_id', customerId)
    .select()
    .single();

  if (error) {
    console.error('Error updating account:', error);
    throw error;
  }

  return data!;
}

// Subscription operations
export async function createSubscription(subscription: Partial<Subscription>): Promise<Subscription> {
  const { data, error } = await supabase
    .from('subscriptions')
    .insert({
      ...subscription,
      created_at: new Date(),
      updated_at: new Date(),
    })
    .select()
    .single();

  if (error) {
    console.error('Error creating subscription:', error);
    throw error;
  }

  return data!;
}

export async function updateSubscription(
  stripeSubscriptionId: string,
  updates: Partial<Subscription>
): Promise<Subscription> {
  const { data, error } = await supabase
    .from('subscriptions')
    .update({
      ...updates,
      updated_at: new Date(),
    })
    .eq('stripe_subscription_id', stripeSubscriptionId)
    .select()
    .single();

  if (error) {
    console.error('Error updating subscription:', error);
    throw error;
  }

  return data!;
}

export async function getSubscriptionByStripeId(stripeSubscriptionId: string): Promise<Subscription | null> {
  const { data, error } = await supabase
    .from('subscriptions')
    .select('*')
    .eq('stripe_subscription_id', stripeSubscriptionId)
    .single();

  if (error) {
    if (error.code === 'PGRST116') { // Not found
      return null;
    }
    console.error('Error fetching subscription:', error);
    throw error;
  }

  return data;
}

export async function getSubscriptionWithInstance(stripeSubscriptionId: string) {
  const { data, error } = await supabase
    .from('subscriptions')
    .select('*, instances(*)')
    .eq('stripe_subscription_id', stripeSubscriptionId)
    .single();

  if (error) {
    console.error('Error fetching subscription with instance:', error);
    throw error;
  }

  return data;
}

// Instance operations
export async function createInstance(instance: Partial<Instance>): Promise<Instance> {
  const { data, error } = await supabase
    .from('instances')
    .insert({
      ...instance,
      created_at: new Date(),
      updated_at: new Date(),
    })
    .select()
    .single();

  if (error) {
    console.error('Error creating instance:', error);
    throw error;
  }

  return data!;
}

export async function updateInstance(
  subscriptionId: string,
  updates: Partial<Instance>
): Promise<Instance> {
  const { data, error } = await supabase
    .from('instances')
    .update({
      ...updates,
      updated_at: new Date(),
    })
    .eq('subscription_id', subscriptionId)
    .select()
    .single();

  if (error) {
    console.error('Error updating instance:', error);
    throw error;
  }

  return data!;
}

export async function getInstanceBySubscriptionId(subscriptionId: string): Promise<Instance | null> {
  const { data, error } = await supabase
    .from('instances')
    .select('*')
    .eq('subscription_id', subscriptionId)
    .single();

  if (error) {
    if (error.code === 'PGRST116') { // Not found
      return null;
    }
    console.error('Error fetching instance:', error);
    throw error;
  }

  return data;
}

// Usage tracking
export async function recordUsage(usage: Partial<UsageRecord>): Promise<UsageRecord> {
  const { data, error } = await supabase
    .from('usage_records')
    .insert({
      ...usage,
      created_at: new Date(),
    })
    .select()
    .single();

  if (error) {
    console.error('Error recording usage:', error);
    throw error;
  }

  return data!;
}

export async function getTodaysUsage(subscriptionId: string): Promise<UsageRecord | null> {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const { data, error } = await supabase
    .from('usage_records')
    .select('*')
    .eq('subscription_id', subscriptionId)
    .gte('date', today.toISOString())
    .single();

  if (error) {
    if (error.code === 'PGRST116') { // Not found
      return null;
    }
    console.error('Error fetching usage:', error);
    throw error;
  }

  return data;
}

// Webhook event tracking (for idempotency)
export async function saveWebhookEvent(event: Stripe.Event): Promise<WebhookEvent | null> {
  // Check if event already exists
  const { data: existing, error: checkError } = await supabase
    .from('webhook_events')
    .select('*')
    .eq('stripe_event_id', event.id)
    .single();

  if (existing && !checkError) {
    return existing;
  }

  // Save new event
  const { data, error } = await supabase
    .from('webhook_events')
    .insert({
      stripe_event_id: event.id,
      event_type: event.type,
      payload: event.data,
      created_at: new Date(),
    })
    .select()
    .single();

  if (error) {
    console.error('Error saving webhook event:', error);
    // Don't throw - allow processing to continue
  }

  return data;
}

export async function markWebhookProcessed(
  eventId: string,
  success: boolean,
  message?: string
): Promise<void> {
  const { error } = await supabase
    .from('webhook_events')
    .update({
      processed_at: new Date(),
      error: success ? null : message,
    })
    .eq('stripe_event_id', eventId);

  if (error) {
    console.error('Error marking webhook as processed:', error);
    // Don't throw - this is not critical
  }
}

// Utility function to check if subscription should be deprovisioned
export async function shouldDeprovisionSubscription(subscription: Subscription): Promise<boolean> {
  // Check if past grace period
  if (subscription.status === 'cancelled' || subscription.status === 'unpaid') {
    const gracePeriodEnd = new Date(subscription.current_period_end);
    gracePeriodEnd.setDate(gracePeriodEnd.getDate() + config.billing.gracePeriodDays);

    return new Date() > gracePeriodEnd;
  }

  return false;
}
