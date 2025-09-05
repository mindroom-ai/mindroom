# MindRoom Scripts

This directory contains utility scripts for the MindRoom SaaS platform.

## Setup Scripts

### `setup/setup-saas.sh`

Main setup script for initializing the SaaS platform services:
- Configures Supabase database and migrations
- Creates Stripe products and pricing tiers
- Sets up webhook endpoints
- Inserts test data for development

**Usage:**
```bash
./scripts/setup/setup-saas.sh
```

### `setup/setup_stripe_products.py`

Standalone Python script for setting up Stripe products:
- Creates 4 subscription tiers (Free, Starter, Professional, Enterprise)
- Sets up pricing for each tier
- Configures webhook endpoints
- Idempotent - safe to run multiple times

**Usage:**
```bash
# Automatically installs dependencies via UV
./scripts/setup/setup_stripe_products.py

# Or with environment variables
export STRIPE_SECRET_KEY="sk_test_..."
export PLATFORM_DOMAIN="mindroom.chat"
./scripts/setup/setup_stripe_products.py
```

### `setup/test_data.sql`

SQL script with test data for development:
- Creates test customer accounts
- Sets up sample subscriptions
- Creates test instances

## Test Scripts

### `test_stripe.py`

Test script to verify Stripe connection and list products.

**Usage:**
```bash
export STRIPE_SECRET_KEY="sk_test_..."
./scripts/test_stripe.py
```

## Requirements

- **UV**: Scripts use UV for automatic dependency management
- **Environment Variables**: Must have `.env` file configured with:
  - `STRIPE_SECRET_KEY`
  - `SUPABASE_URL`
  - `SUPABASE_ANON_KEY`
  - `PLATFORM_DOMAIN`

## Notes

- All Python scripts use UV's inline script dependencies
- Scripts are idempotent and safe to run multiple times
- Stripe webhook secrets cannot be retrieved after creation
