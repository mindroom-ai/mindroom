# Stripe Handler Testing Guide

## ✅ Tests Completed Without Stripe

### 1. **TypeScript Compilation** ✅
- All TypeScript files compile successfully
- No type errors
- Strict mode enabled

### 2. **Server Startup** ✅
- Server starts on configured port
- Graceful shutdown with SIGTERM
- Environment variable loading works
- Test environment configuration works

### 3. **Health Check Endpoint** ✅
```bash
curl http://localhost:3006/health
```
Returns server status with uptime

### 4. **Webhook Security** ✅
- Rejects webhooks without signature (400)
- Rejects webhooks with invalid signature (400)
- Proper error messages returned

### 5. **Code Quality** ✅
- All unused variables fixed
- Proper error handling
- TypeScript strict mode compliance

## 🔄 Tests That Require Stripe Account

### 1. **Real Webhook Signature Verification**
Requires actual Stripe webhook secret to generate valid signatures

### 2. **Subscription Creation Flow**
- Customer creation
- Subscription provisioning
- Instance creation
- Email sending

### 3. **Payment Processing**
- Invoice payment success
- Payment failure handling
- Grace period management

### 4. **Tier Changes**
- Upgrade flow
- Downgrade flow
- Resource limit updates

## 🧪 Testing With Stripe CLI

Once you have Stripe credentials:

### 1. Install Stripe CLI
```bash
# macOS
brew install stripe/stripe-cli/stripe

# Linux
curl -s https://packages.stripe.dev/api/security/keypair | gpg --dearmor | sudo tee /usr/share/keyrings/stripe.gpg
echo "deb [signed-by=/usr/share/keyrings/stripe.gpg] https://packages.stripe.dev/stripe-cli-debian-local stable main" | sudo tee -a /etc/apt/sources.list.d/stripe.list
sudo apt update
sudo apt install stripe
```

### 2. Configure and Forward Webhooks
```bash
# Login to Stripe
stripe login

# Forward webhooks to local server
stripe listen --forward-to localhost:3005/webhooks/stripe

# Copy the webhook signing secret to .env
# It will look like: whsec_...
```

### 3. Trigger Test Events
```bash
# Test subscription creation
stripe trigger customer.subscription.created

# Test payment success
stripe trigger invoice.payment_succeeded

# Test payment failure
stripe trigger invoice.payment_failed

# Test subscription cancellation
stripe trigger customer.subscription.deleted
```

## 🐳 Testing with Docker

### 1. Build and Run
```bash
# Build image
docker build -t mindroom-stripe-handler .

# Run with test environment
docker run -p 3006:3006 --env-file .env.test mindroom-stripe-handler
```

### 2. Docker Compose Testing
```bash
# Start with development profile (includes Stripe CLI)
docker-compose --profile development up

# The Stripe CLI container will automatically forward webhooks
```

## 🔍 Testing with Mock Services

For integration testing without external services:

### 1. Mock Supabase
- Returns success for all database operations
- Tracks call history for verification

### 2. Mock Provisioner
- Returns predefined responses
- Simulates provisioning delay

### 3. Mock Email Service
- Logs email sends without actually sending
- Validates email templates

## 📊 What's Working Now

Without Stripe credentials, we've verified:

1. **Server Infrastructure** ✅
   - Express server starts correctly
   - Routes are configured
   - Middleware is working
   - Error handling is in place

2. **TypeScript Setup** ✅
   - All types are defined
   - Strict mode compliance
   - Build process works

3. **Configuration** ✅
   - Environment variables load correctly
   - Test vs production separation
   - Validation on startup

4. **Security** ✅
   - Webhook signature verification code is in place
   - Rejects invalid/missing signatures
   - Raw body parsing for signature verification

5. **Code Organization** ✅
   - Clean separation of concerns
   - Handlers, services, routes separated
   - Type safety throughout

## 🚀 Next Steps

1. **Get Stripe Account**
   - Sign up at https://stripe.com
   - Create products and prices
   - Configure webhook endpoint

2. **Set Up Supabase**
   - Create project at https://supabase.com
   - Run migration scripts to create tables
   - Get service role key

3. **Deploy Provisioner Service**
   - Set up Dokku server
   - Deploy provisioner service
   - Configure API authentication

4. **Production Deployment**
   - Deploy to production server
   - Configure production environment variables
   - Set up monitoring and logging
   - Configure Stripe production webhook

## 📝 Manual Test Checklist

When you have all services ready:

- [ ] Stripe webhook endpoint configured
- [ ] Supabase tables created
- [ ] Provisioner service running
- [ ] Email service configured
- [ ] Create test subscription
- [ ] Verify instance provisioned
- [ ] Test payment success
- [ ] Test payment failure
- [ ] Test subscription upgrade
- [ ] Test subscription downgrade
- [ ] Test subscription cancellation
- [ ] Verify emails sent correctly
- [ ] Check idempotency (send same webhook twice)
- [ ] Test grace period enforcement
- [ ] Monitor error logs
