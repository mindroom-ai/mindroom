import Stripe from 'stripe';
import { config } from '../config';
import {
  getSubscriptionByStripeId,
  updateSubscription,
  updateInstance,
  getInstanceBySubscriptionId,
  recordUsage,
} from '../services/supabase';
import { sendEmail } from '../services/email';
import {
  ProcessingResult,
  WebhookProcessingContext,
} from '../types';

export async function handleInvoicePaymentSucceeded(
  invoice: Stripe.Invoice,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing invoice.payment_succeeded: ${invoice.id}`);

  try {
    // Skip if this is not a subscription invoice
    if (!invoice.subscription) {
      return {
        success: true,
        message: 'Not a subscription invoice, skipping',
      };
    }

    const subscriptionId = invoice.subscription as string;

    // Update subscription payment status
    const subscription = await getSubscriptionByStripeId(subscriptionId);
    if (!subscription) {
      console.warn(`Subscription not found for invoice: ${subscriptionId}`);
      return {
        success: false,
        message: 'Subscription not found',
      };
    }

    // Update subscription status to active if it was past_due or incomplete
    if (['past_due', 'incomplete', 'unpaid'].includes(subscription.status)) {
      await updateSubscription(subscriptionId, {
        status: 'active',
      });

      // Reactivate instance if it was stopped
      const instance = await getInstanceBySubscriptionId(subscription.id);
      if (instance && instance.status === 'stopped') {
        await updateInstance(subscription.id, {
          status: 'active',
        });

        console.log(`✅ Reactivated instance for subscription ${subscriptionId}`);
      }
    }

    // Record successful payment for analytics
    await recordUsage({
      subscription_id: subscription.id,
      date: new Date(),
      messages_sent: 0, // Will be updated by usage tracking
      agents_active: 0, // Will be updated by usage tracking
      api_calls: 0, // Will be updated by usage tracking
    });

    // Log successful payment
    console.log(`✅ Payment successful for subscription ${subscriptionId}: $${(invoice.amount_paid / 100).toFixed(2)}`);

    return {
      success: true,
      message: `Payment of $${(invoice.amount_paid / 100).toFixed(2)} processed successfully`,
      data: {
        subscriptionId: subscription.id,
        amountPaid: invoice.amount_paid,
      },
    };
  } catch (error: any) {
    console.error('Error handling invoice.payment_succeeded:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}

export async function handleInvoicePaymentFailed(
  invoice: Stripe.Invoice,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing invoice.payment_failed: ${invoice.id}`);

  try {
    // Skip if this is not a subscription invoice
    if (!invoice.subscription) {
      return {
        success: true,
        message: 'Not a subscription invoice, skipping',
      };
    }

    const subscriptionId = invoice.subscription as string;
    const attemptCount = invoice.attempt_count || 1;

    // Update subscription payment status
    const subscription = await getSubscriptionByStripeId(subscriptionId);
    if (!subscription) {
      console.warn(`Subscription not found for invoice: ${subscriptionId}`);
      return {
        success: false,
        message: 'Subscription not found',
      };
    }

    // Update subscription status
    await updateSubscription(subscriptionId, {
      status: attemptCount >= 3 ? 'unpaid' : 'past_due',
    });

    // Send payment failed email
    await sendEmail({
      to: invoice.customer_email || '',
      subject: '',
      template: 'payment_failed',
      data: {
        gracePeriodDays: config.billing.gracePeriodDays,
        billingUrl: `https://mindroom.chat/billing?customer=${invoice.customer}`,
        attemptCount: attemptCount,
      },
    });

    // If this is the final attempt, consider suspending the instance
    if (attemptCount >= 3) {
      const instance = await getInstanceBySubscriptionId(subscription.id);
      if (instance && instance.status === 'active') {
        // Calculate grace period end date
        const gracePeriodEnd = new Date();
        gracePeriodEnd.setDate(gracePeriodEnd.getDate() + config.billing.gracePeriodDays);

        console.log(`⚠️ Subscription ${subscriptionId} will be suspended on ${gracePeriodEnd.toLocaleDateString()}`);

        // Note: We don't immediately suspend - we wait for the grace period
        // This would be handled by a scheduled job
      }
    }

    console.log(`❌ Payment failed for subscription ${subscriptionId} (attempt ${attemptCount})`);

    return {
      success: true,
      message: `Payment failure processed (attempt ${attemptCount})`,
      data: {
        subscriptionId: subscription.id,
        attemptCount: attemptCount,
        status: attemptCount >= 3 ? 'final_attempt' : 'will_retry',
      },
    };
  } catch (error: any) {
    console.error('Error handling invoice.payment_failed:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}

export async function handleInvoiceUpcoming(
  invoice: Stripe.Invoice,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing invoice.upcoming: ${invoice.id}`);

  try {
    // Skip if this is not a subscription invoice
    if (!invoice.subscription) {
      return {
        success: true,
        message: 'Not a subscription invoice, skipping',
      };
    }

    // This webhook is useful for:
    // 1. Sending payment reminder emails
    // 2. Checking and enforcing usage limits before renewal
    // 3. Adding usage-based charges to the invoice

    const subscriptionId = invoice.subscription as string;
    const subscription = await getSubscriptionByStripeId(subscriptionId);

    if (!subscription) {
      console.warn(`Subscription not found for upcoming invoice: ${subscriptionId}`);
      return {
        success: false,
        message: 'Subscription not found',
      };
    }

    // TODO: Add usage-based billing logic here
    // Example: Check if customer exceeded message limits and add charges

    console.log(`📧 Upcoming invoice for subscription ${subscriptionId}: $${(invoice.amount_due / 100).toFixed(2)}`);

    return {
      success: true,
      message: 'Upcoming invoice processed',
      data: {
        subscriptionId: subscription.id,
        amountDue: invoice.amount_due,
        nextPaymentAttempt: invoice.next_payment_attempt ? new Date(invoice.next_payment_attempt * 1000) : null,
      },
    };
  } catch (error: any) {
    console.error('Error handling invoice.upcoming:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}
