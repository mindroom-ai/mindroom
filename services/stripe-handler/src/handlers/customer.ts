import Stripe from 'stripe';
import {
  getOrCreateAccount,
  updateAccount,
} from '../services/supabase';
import {
  ProcessingResult,
  WebhookProcessingContext,
} from '../types';

export async function handleCustomerCreated(
  customer: Stripe.Customer,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing customer.created: ${customer.id}`);

  try {
    // Create account record if it doesn't exist
    const account = await getOrCreateAccount(customer.id, customer.email || undefined);

    console.log(`✅ Customer account created/verified: ${account.email}`);

    return {
      success: true,
      message: `Customer account created for ${customer.email}`,
      data: {
        accountId: account.id,
        email: account.email,
      },
    };
  } catch (error: any) {
    console.error('Error handling customer.created:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}

export async function handleCustomerUpdated(
  customer: Stripe.Customer,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing customer.updated: ${customer.id}`);

  try {
    // Update account information
    const account = await updateAccount(customer.id, {
      email: customer.email || '',
    });

    console.log(`✅ Customer account updated: ${account.email}`);

    return {
      success: true,
      message: `Customer account updated for ${customer.email}`,
      data: {
        accountId: account.id,
        email: account.email,
      },
    };
  } catch (error: any) {
    console.error('Error handling customer.updated:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}

export async function handleCustomerDeleted(
  customer: Stripe.Customer | Stripe.DeletedCustomer,
  _context: WebhookProcessingContext
): Promise<ProcessingResult> {
  console.log(`Processing customer.deleted: ${customer.id}`);

  try {
    // Note: We don't actually delete the account record
    // We might want to mark it as deleted or archive it
    // This depends on your data retention policy

    console.log(`⚠️ Customer deleted in Stripe: ${customer.id}`);
    console.log('Account record retained for audit purposes');

    return {
      success: true,
      message: `Customer deletion processed (account retained)`,
      data: {
        customerId: customer.id,
        action: 'retained_for_audit',
      },
    };
  } catch (error: any) {
    console.error('Error handling customer.deleted:', error);
    return {
      success: false,
      message: error.message,
      error: error,
    };
  }
}
