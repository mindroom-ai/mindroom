#!/usr/bin/env node

/**
 * Script to set up Stripe products and prices for MindRoom
 * Run this once to create the products in your Stripe account
 */

const Stripe = require('stripe');
require('dotenv').config();

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);

async function createProducts() {
  console.log('🚀 Setting up Stripe products and prices...\n');

  try {
    // Create the main product
    console.log('Creating MindRoom product...');
    const product = await stripe.products.create({
      name: 'MindRoom Subscription',
      description: 'AI-powered team collaboration platform',
      metadata: {
        platform: 'mindroom',
      },
    });
    console.log('✅ Product created:', product.id);

    // Create prices for each tier
    const tiers = [
      {
        nickname: 'Starter',
        unit_amount: 4900, // $49.00
        metadata: { tier: 'starter' },
        lookup_key: 'starter',
      },
      {
        nickname: 'Professional',
        unit_amount: 19900, // $199.00
        metadata: { tier: 'professional' },
        lookup_key: 'professional',
      },
    ];

    console.log('\nCreating prices...');
    const prices = [];

    for (const tier of tiers) {
      const price = await stripe.prices.create({
        product: product.id,
        nickname: tier.nickname,
        currency: 'usd',
        recurring: {
          interval: 'month',
        },
        unit_amount: tier.unit_amount,
        metadata: tier.metadata,
        lookup_key: tier.lookup_key,
      });

      prices.push(price);
      console.log(`✅ ${tier.nickname} price created:`, price.id);
    }

    // Output environment variables to set
    console.log('\n📝 Add these to your .env file:\n');
    console.log('# Stripe Price IDs');
    console.log(`STRIPE_PRICE_STARTER=${prices[0].id}`);
    console.log(`STRIPE_PRICE_PROFESSIONAL=${prices[1].id}`);

    console.log('\n✨ Setup complete!');

  } catch (error) {
    console.error('❌ Error setting up products:', error.message);
    process.exit(1);
  }
}

// Run the setup
createProducts();
