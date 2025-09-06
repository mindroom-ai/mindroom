import Stripe from 'stripe';
import { config, TierName } from '../config';
import {
  getOrCreateAccount,
  createSubscription,
  updateSubscription,
  getSubscriptionByStripeId,
  getSubscriptionWithInstance,
  createInstance,
  updateInstance,
  getInstanceBySubscriptionId,
} from '../services/supabase';
import {
  provisionInstance,
  deprovisionInstance,
  updateInstanceLimits,
  getResourceLimits,
  getTierFromPriceId,
} from '../services/provisioner';
import { sendEmail } from '../services/email';
import {
  ProcessingResult,
  WebhookProcessingContext,
  Subscription,
} from '../types';

export async function handleSubscriptionCreated(
  subscription: Stripe.Subscription,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing subscription.created: ${subscription.id}`);

  try {
    // Extract customer information
    const customerId = subscription.customer as string;
    const customerEmail = subscription.metadata?.email || '';

    // Get or create account
    const account = await getOrCreateAccount(customerId, customerEmail);

    // Determine tier from price ID
    const priceId = subscription.items.data[0].price.id;
    const tier = getTierFromPriceId(priceId) as TierName;
    const limits = getResourceLimits(tier);

    // Create subscription record
    const sub = await createSubscription({
      account_id: account.id,
      stripe_subscription_id: subscription.id,
      stripe_price_id: priceId,
      tier: tier,
      status: subscription.status as Subscription['status'],
      max_agents: limits.agents,
      max_messages_per_day: limits.messagesPerDay,
      current_period_start: new Date(subscription.current_period_start * 1000),
      current_period_end: new Date(subscription.current_period_end * 1000),
      trial_ends_at: subscription.trial_end ? new Date(subscription.trial_end * 1000) : null,
    });

    // Provision instance
    const provisionResult = await provisionInstance({
      subscriptionId: sub.id,
      accountId: account.id,
      tier: tier,
      limits: limits,
    });

    // Create instance record
    await createInstance({
      subscription_id: sub.id,
      dokku_app_name: provisionResult.appName,
      subdomain: provisionResult.subdomain,
      status: 'provisioning',
      frontend_url: provisionResult.frontendUrl,
      backend_url: provisionResult.backendUrl,
      memory_limit_mb: limits.memoryMb,
      cpu_limit: limits.cpuLimit,
    });

    // Send welcome email
    await sendEmail({
      to: account.email,
      subject: 'Welcome to MindRoom!',
      template: 'welcome',
      data: {
        instanceUrl: provisionResult.frontendUrl,
        tier: tier,
      },
    });

    return {
      success: true,
      message: `Subscription created and instance provisioned for ${account.email}`,
      data: {
        subscriptionId: sub.id,
        instanceUrl: provisionResult.frontendUrl,
      },
    };
  } catch (error: any) {
    console.error('Error handling subscription.created:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}

export async function handleSubscriptionUpdated(
  subscription: Stripe.Subscription,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing subscription.updated: ${subscription.id}`);

  try {
    // Get existing subscription
    const existingSub = await getSubscriptionByStripeId(subscription.id);
    if (!existingSub) {
      console.warn(`Subscription not found: ${subscription.id}`);
      return {
        success: false,
        message: 'Subscription not found in database',
      };
    }

    // Determine new tier
    const priceId = subscription.items.data[0].price.id;
    const newTier = getTierFromPriceId(priceId) as TierName;
    const newLimits = getResourceLimits(newTier);

    // Check if tier changed
    const tierChanged = existingSub.tier !== newTier;

    // Update subscription record
    const updatedSub = await updateSubscription(subscription.id, {
      stripe_price_id: priceId,
      tier: newTier,
      status: subscription.status as Subscription['status'],
      max_agents: newLimits.agents,
      max_messages_per_day: newLimits.messagesPerDay,
      current_period_start: new Date(subscription.current_period_start * 1000),
      current_period_end: new Date(subscription.current_period_end * 1000),
      trial_ends_at: subscription.trial_end ? new Date(subscription.trial_end * 1000) : null,
    });

    // Update instance resources if tier changed
    if (tierChanged) {
      const instance = await getInstanceBySubscriptionId(updatedSub.id);

      if (instance) {
        // Update resource limits in provisioner
        await updateInstanceLimits({
          appName: instance.dokku_app_name,
          limits: newLimits,
        });

        // Update instance record
        await updateInstance(updatedSub.id, {
          memory_limit_mb: newLimits.memoryMb,
          cpu_limit: newLimits.cpuLimit,
        });

        // Send email about tier change
        const emailTemplate = newTier > existingSub.tier ? 'subscription_upgraded' : 'subscription_downgraded';
        await sendEmail({
          to: subscription.metadata?.email || '',
          subject: '',
          template: emailTemplate,
          data: {
            newTier: newTier,
            agents: newLimits.agents,
            messagesPerDay: newLimits.messagesPerDay,
            memoryMb: newLimits.memoryMb,
            cpuLimit: newLimits.cpuLimit,
          },
        });
      }
    }

    return {
      success: true,
      message: `Subscription updated ${tierChanged ? `(tier: ${existingSub.tier} → ${newTier})` : ''}`,
      data: {
        subscriptionId: updatedSub.id,
        tierChanged: tierChanged,
      },
    };
  } catch (error: any) {
    console.error('Error handling subscription.updated:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}

export async function handleSubscriptionDeleted(
  subscription: Stripe.Subscription,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing subscription.deleted: ${subscription.id}`);

  try {
    // Get subscription with instance
    const sub = await getSubscriptionWithInstance(subscription.id);
    if (!sub) {
      console.warn(`Subscription not found: ${subscription.id}`);
      return {
        success: false,
        message: 'Subscription not found in database',
      };
    }

    // Update subscription status
    await updateSubscription(subscription.id, {
      status: 'cancelled',
      cancelled_at: new Date(),
    });

    // Deprovision instance if exists
    if (sub.instances && sub.instances.length > 0) {
      const instance = sub.instances[0];

      // Initiate deprovisioning
      await deprovisionInstance({
        appName: instance.dokku_app_name,
      });

      // Update instance status
      await updateInstance(sub.id, {
        status: 'deprovisioning',
        deprovisioned_at: new Date(),
      });
    }

    // Send cancellation email
    const accessEndDate = new Date(subscription.current_period_end * 1000);
    accessEndDate.setDate(accessEndDate.getDate() + config.billing.gracePeriodDays);

    await sendEmail({
      to: subscription.metadata?.email || '',
      subject: '',
      template: 'subscription_cancelled',
      data: {
        accessEndDate: accessEndDate.toLocaleDateString(),
        reactivateUrl: `https://mindroom.chat/billing/reactivate?customer=${subscription.customer}`,
      },
    });

    return {
      success: true,
      message: `Subscription cancelled and instance scheduled for deprovisioning`,
      data: {
        subscriptionId: sub.id,
        deprovisionDate: accessEndDate,
      },
    };
  } catch (error: any) {
    console.error('Error handling subscription.deleted:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}

export async function handleSubscriptionTrialWillEnd(
  subscription: Stripe.Subscription,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing subscription.trial_will_end: ${subscription.id}`);

  try {
    // Send reminder email
    await sendEmail({
      to: subscription.metadata?.email || '',
      subject: '',
      template: 'trial_ending',
      data: {
        trialEndDate: subscription.trial_end ? new Date(subscription.trial_end * 1000).toLocaleDateString() : 'soon',
        billingUrl: `https://mindroom.chat/billing?customer=${subscription.customer}`,
      },
    });

    return {
      success: true,
      message: 'Trial ending reminder sent',
    };
  } catch (error: any) {
    console.error('Error handling subscription.trial_will_end:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}
