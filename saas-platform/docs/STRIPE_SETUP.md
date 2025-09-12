# Stripe Integration Setup Guide

This guide explains how to set up Stripe for the MindRoom SaaS platform.

## Overview

The MindRoom platform uses Stripe for subscription billing with the following plans:
- **Free**: $0/month - Basic features
- **Starter**: $10/month or $96/year (20% discount) - Perfect for individuals
- **Professional**: $8/user/month or $76.80/user/year (20% discount) - For teams
- **Enterprise**: Custom pricing - Contact sales

## Initial Setup

### 1. Environment Variables

Add these to your `.env` file:
```bash
# Required Stripe keys (get from Stripe Dashboard)
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

### 2. Run the Setup Script

The setup script automatically:
- Creates/updates the MindRoom product in Stripe
- Creates price objects for each plan
- Updates `pricing-config.yaml` with Stripe price IDs

```bash
cd saas-platform
python scripts/setup_stripe_prices.py
```

Output should look like:
```
✅ Using Stripe API key: sk_test_51S3...
🚀 Setting up Stripe prices for MindRoom SaaS platform
============================================================

🔍 Looking for existing MindRoom product...
✅ Found existing product: prod_XXX - MindRoom Subscription

💰 Setting up price for starter (month)...
   Amount: $10.00 per month
   ✅ Created price: price_XXX

[... more prices ...]

✅ Stripe setup complete!
```

### 3. Configure Webhooks

In your Stripe Dashboard:

1. Go to **Developers** → **Webhooks**
2. Add endpoint:
   - **Endpoint URL**: `https://api.staging.mindroom.chat/webhooks/stripe`
   - **Events to listen for**:
     - `customer.subscription.created`
     - `customer.subscription.updated`
     - `customer.subscription.deleted`
     - `invoice.payment_succeeded`
     - `invoice.payment_failed`
     - `customer.subscription.trial_will_end`
3. Copy the **Signing secret** to your `.env` as `STRIPE_WEBHOOK_SECRET`

## Testing

### 1. Verify Configuration

Run the Stripe utility tests:
```bash
cd platform-backend
STRIPE_SECRET_KEY=sk_test_... pytest tests/test_stripe_utils.py -xvs
```

### 2. Test Checkout Flow

1. Navigate to the upgrade page: `/dashboard/billing/upgrade`
2. Select a plan and billing cycle
3. Click "Continue to Checkout"
4. Complete the Stripe checkout form (use test card: 4242 4242 4242 4242)

### 3. Test Webhooks

Use Stripe CLI to test webhooks locally:
```bash
stripe listen --forward-to localhost:8765/webhooks/stripe
stripe trigger customer.subscription.created
```

## Security Notes

### Safe to Commit
- ✅ Stripe price IDs (`price_XXX...`) - Public identifiers
- ✅ Stripe product IDs (`prod_XXX...`) - Public identifiers
- ✅ Webhook endpoint URLs

### NEVER Commit
- ❌ Secret keys (`sk_test_...`, `sk_live_...`)
- ❌ Webhook signing secrets (`whsec_...`)
- ❌ Any API keys

## Troubleshooting

### "No price found" Error
Run `python scripts/setup_stripe_prices.py` to configure prices.

### Webhook Signature Verification Failed
Ensure `STRIPE_WEBHOOK_SECRET` matches the signing secret in Stripe Dashboard.

### Price Mismatch
The script automatically archives old prices and creates new ones if amounts change.

### Test vs Production
- Test keys start with `_test_`
- Production keys start with `_live_`
- Always use test keys for development/staging

## Price ID Management

Price IDs are stored in `pricing-config.yaml`:
```yaml
plans:
  starter:
    stripe_price_id_monthly: price_XXX
    stripe_price_id_yearly: price_YYY
  professional:
    stripe_price_id_monthly: price_ZZZ
    stripe_price_id_yearly: price_WWW
```

These are automatically managed by the setup script and safe to commit.

## Going to Production

1. Get production API keys from Stripe
2. Update `.env` with production keys
3. Run setup script with production keys
4. Update webhook endpoint URL to production domain
5. Test thoroughly before going live
