import { Router, Request, Response } from 'express';
import Stripe from 'stripe';
import { config } from '../config';
import {
  handleSubscriptionCreated,
  handleSubscriptionUpdated,
  handleSubscriptionDeleted,
  handleSubscriptionTrialWillEnd
} from '../handlers/subscription';
import {
  handleInvoicePaymentSucceeded,
  handleInvoicePaymentFailed,
  handleInvoiceUpcoming
} from '../handlers/invoice';
import {
  handleCustomerCreated,
  handleCustomerUpdated,
  handleCustomerDeleted
} from '../handlers/customer';
import { saveWebhookEvent, markWebhookProcessed } from '../services/supabase';
import { WebhookProcessingContext, StripeEventType } from '../types';

const stripe = new Stripe(config.stripe.secretKey, {
  apiVersion: '2023-10-16',
});

export const webhookRouter = Router();

// Main webhook endpoint
webhookRouter.post('/stripe', async (req: Request, res: Response): Promise<Response> => {
  const sig = req.headers['stripe-signature'] as string;

  if (!sig) {
    console.error('Missing stripe-signature header');
    return res.status(400).send('Missing stripe-signature header');
  }

  let event: Stripe.Event;

  // Verify webhook signature
  try {
    event = stripe.webhooks.constructEvent(
      req.body,
      sig,
      config.stripe.webhookSecret
    );
  } catch (err: any) {
    console.error('Webhook signature verification failed:', err.message);
    return res.status(400).send(`Webhook Error: ${err.message}`);
  }

  // Create processing context
  const context: WebhookProcessingContext = {
    eventId: event.id,
    timestamp: new Date(event.created * 1000),
    retryCount: 0,
  };

  // Log webhook event
  console.log(`📨 Received webhook: ${event.type} (${event.id})`);

  // Save event to database for idempotency and audit trail
  try {
    const existingEvent = await saveWebhookEvent(event);
    if (existingEvent?.processed_at) {
      console.log(`Event ${event.id} already processed, skipping`);
      return res.json({ received: true, status: 'already_processed' });
    }
  } catch (error) {
    console.error('Error saving webhook event:', error);
    // Continue processing even if save fails
  }

  // Process the event
  try {
    let result;

    switch (event.type as StripeEventType) {
      // Subscription events
      case 'customer.subscription.created':
        result = await handleSubscriptionCreated(event.data.object as Stripe.Subscription, context);
        break;

      case 'customer.subscription.updated':
        result = await handleSubscriptionUpdated(event.data.object as Stripe.Subscription, context);
        break;

      case 'customer.subscription.deleted':
        result = await handleSubscriptionDeleted(event.data.object as Stripe.Subscription, context);
        break;

      case 'customer.subscription.trial_will_end':
        result = await handleSubscriptionTrialWillEnd(event.data.object as Stripe.Subscription, context);
        break;

      // Invoice events
      case 'invoice.payment_succeeded':
        result = await handleInvoicePaymentSucceeded(event.data.object as Stripe.Invoice, context);
        break;

      case 'invoice.payment_failed':
        result = await handleInvoicePaymentFailed(event.data.object as Stripe.Invoice, context);
        break;

      case 'invoice.upcoming':
        result = await handleInvoiceUpcoming(event.data.object as Stripe.Invoice, context);
        break;

      // Customer events
      case 'customer.created':
        result = await handleCustomerCreated(event.data.object as Stripe.Customer, context);
        break;

      case 'customer.updated':
        result = await handleCustomerUpdated(event.data.object as Stripe.Customer, context);
        break;

      case 'customer.deleted':
        result = await handleCustomerDeleted(event.data.object as Stripe.Customer, context);
        break;

      default:
        console.log(`⚠️ Unhandled event type: ${event.type}`);
        result = { success: true, message: 'Event type not handled' };
    }

    // Mark event as processed
    await markWebhookProcessed(event.id, result?.success || false, result?.message);

    console.log(`✅ Successfully processed ${event.type}`);
    return res.json({ received: true, status: 'processed', result });

  } catch (error: any) {
    console.error(`❌ Error processing webhook ${event.type}:`, error);

    // Save error to database
    try {
      await markWebhookProcessed(event.id, false, error.message);
    } catch (dbError) {
      console.error('Failed to save error to database:', dbError);
    }

    // Return 500 to trigger Stripe retry
    return res.status(500).send({
      error: 'Error processing webhook',
      message: process.env.NODE_ENV === 'development' ? error.message : undefined
    });
  }
});

// Test endpoint (development and test only)
if (process.env.NODE_ENV === 'development' || process.env.NODE_ENV === 'test') {
  webhookRouter.get('/test', (_req: Request, res: Response) => {
    res.json({
      status: 'ok',
      message: 'Webhook endpoint is working',
      config: {
        stripeConfigured: !!config.stripe.secretKey,
        supabaseConfigured: !!config.supabase.url,
        provisionerConfigured: !!config.provisioner.url,
      }
    });
  });
}
