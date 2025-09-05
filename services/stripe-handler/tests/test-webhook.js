#!/usr/bin/env node

/**
 * Test webhook script for manual testing
 * Usage: node tests/test-webhook.js [event-type]
 */

const crypto = require('crypto');
const https = require('https');
const http = require('http');

// Configuration
const WEBHOOK_URL = process.env.WEBHOOK_URL || 'http://localhost:3005/webhooks/stripe';
const WEBHOOK_SECRET = process.env.STRIPE_WEBHOOK_SECRET || 'whsec_test_secret';

// Sample webhook payloads
const sampleEvents = {
  'customer.subscription.created': {
    id: 'evt_test_subscription_created',
    object: 'event',
    api_version: '2023-10-16',
    created: Math.floor(Date.now() / 1000),
    type: 'customer.subscription.created',
    data: {
      object: {
        id: 'sub_test_123',
        object: 'subscription',
        customer: 'cus_test_123',
        status: 'active',
        items: {
          data: [{
            price: {
              id: 'price_starter',
              product: 'prod_test_123',
            }
          }]
        },
        current_period_start: Math.floor(Date.now() / 1000),
        current_period_end: Math.floor(Date.now() / 1000) + 30 * 24 * 60 * 60,
        trial_end: null,
        metadata: {
          email: 'test@example.com'
        }
      }
    }
  },

  'invoice.payment_succeeded': {
    id: 'evt_test_payment_succeeded',
    object: 'event',
    api_version: '2023-10-16',
    created: Math.floor(Date.now() / 1000),
    type: 'invoice.payment_succeeded',
    data: {
      object: {
        id: 'in_test_123',
        object: 'invoice',
        customer: 'cus_test_123',
        subscription: 'sub_test_123',
        amount_paid: 4900,
        customer_email: 'test@example.com',
        attempt_count: 1,
      }
    }
  },

  'invoice.payment_failed': {
    id: 'evt_test_payment_failed',
    object: 'event',
    api_version: '2023-10-16',
    created: Math.floor(Date.now() / 1000),
    type: 'invoice.payment_failed',
    data: {
      object: {
        id: 'in_test_456',
        object: 'invoice',
        customer: 'cus_test_123',
        subscription: 'sub_test_123',
        amount_due: 4900,
        customer_email: 'test@example.com',
        attempt_count: 1,
      }
    }
  },

  'customer.subscription.deleted': {
    id: 'evt_test_subscription_deleted',
    object: 'event',
    api_version: '2023-10-16',
    created: Math.floor(Date.now() / 1000),
    type: 'customer.subscription.deleted',
    data: {
      object: {
        id: 'sub_test_123',
        object: 'subscription',
        customer: 'cus_test_123',
        status: 'cancelled',
        current_period_end: Math.floor(Date.now() / 1000) + 7 * 24 * 60 * 60,
        metadata: {
          email: 'test@example.com'
        }
      }
    }
  }
};

// Generate Stripe signature
function generateSignature(payload, secret) {
  const timestamp = Math.floor(Date.now() / 1000);
  const signedPayload = `${timestamp}.${payload}`;
  const signature = crypto
    .createHmac('sha256', secret)
    .update(signedPayload)
    .digest('hex');

  return `t=${timestamp},v1=${signature}`;
}

// Send webhook
function sendWebhook(eventType) {
  const event = sampleEvents[eventType];

  if (!event) {
    console.error(`Unknown event type: ${eventType}`);
    console.log('Available events:', Object.keys(sampleEvents).join(', '));
    process.exit(1);
  }

  const payload = JSON.stringify(event);
  const signature = generateSignature(payload, WEBHOOK_SECRET);

  const url = new URL(WEBHOOK_URL);
  const options = {
    hostname: url.hostname,
    port: url.port || (url.protocol === 'https:' ? 443 : 80),
    path: url.pathname,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(payload),
      'stripe-signature': signature,
    }
  };

  console.log(`Sending ${eventType} webhook to ${WEBHOOK_URL}...`);

  const client = url.protocol === 'https:' ? https : http;
  const req = client.request(options, (res) => {
    let data = '';

    res.on('data', (chunk) => {
      data += chunk;
    });

    res.on('end', () => {
      console.log(`Response status: ${res.statusCode}`);
      console.log('Response body:', data);
    });
  });

  req.on('error', (error) => {
    console.error('Error sending webhook:', error.message);
  });

  req.write(payload);
  req.end();
}

// Main
const eventType = process.argv[2] || 'customer.subscription.created';
sendWebhook(eventType);
