import dotenv from 'dotenv';
import path from 'path';

// Load environment variables from .env file
const envFile = process.env.NODE_ENV === 'test' ? '.env.test' : '.env';
dotenv.config({ path: path.join(__dirname, '..', envFile) });

export const config = {
  port: process.env.PORT || 3005,
  nodeEnv: process.env.NODE_ENV || 'development',

  stripe: {
    secretKey: process.env.STRIPE_SECRET_KEY!,
    webhookSecret: process.env.STRIPE_WEBHOOK_SECRET!,

    // Price IDs for different tiers
    prices: {
      starter: process.env.STRIPE_PRICE_STARTER || 'price_starter',
      professional: process.env.STRIPE_PRICE_PROFESSIONAL || 'price_professional',
      enterprise: process.env.STRIPE_PRICE_ENTERPRISE || 'price_enterprise',
    },

    // Webhook configuration
    webhookTolerance: parseInt(process.env.STRIPE_WEBHOOK_TOLERANCE || '300', 10), // seconds
    retryAttempts: parseInt(process.env.STRIPE_RETRY_ATTEMPTS || '3', 10),
  },

  supabase: {
    url: process.env.SUPABASE_URL!,
    serviceKey: process.env.SUPABASE_SERVICE_KEY!,
  },

  provisioner: {
    url: process.env.INSTANCE_PROVISIONER_URL || 'http://localhost:8002',
    apiKey: process.env.PROVISIONER_API_KEY,
    timeout: parseInt(process.env.PROVISIONER_TIMEOUT || '30000', 10), // milliseconds
  },

  email: {
    provider: process.env.EMAIL_PROVIDER || 'resend',
    apiKey: process.env.RESEND_API_KEY || process.env.EMAIL_API_KEY,
    fromAddress: process.env.EMAIL_FROM || 'noreply@mindroom.chat',
    fromName: process.env.EMAIL_FROM_NAME || 'MindRoom',
  },

  // Business logic configuration
  billing: {
    gracePeriodDays: parseInt(process.env.GRACE_PERIOD_DAYS || '7', 10),
    trialDays: parseInt(process.env.TRIAL_DAYS || '14', 10),
  },

  // Resource limits by tier
  tiers: {
    free: {
      agents: 1,
      messagesPerDay: 100,
      memoryMb: 256,
      cpuLimit: 0.25,
      price: 0,
    },
    starter: {
      agents: 3,
      messagesPerDay: 1000,
      memoryMb: 512,
      cpuLimit: 0.5,
      price: 49,
    },
    professional: {
      agents: 10,
      messagesPerDay: 10000,
      memoryMb: 2048,
      cpuLimit: 2,
      price: 199,
    },
    enterprise: {
      agents: -1, // unlimited
      messagesPerDay: -1, // unlimited
      memoryMb: 8192,
      cpuLimit: 4,
      price: -1, // custom pricing
    },
  },
};

// Validation
const validateConfig = () => {
  const required = {
    'STRIPE_SECRET_KEY': config.stripe.secretKey,
    'STRIPE_WEBHOOK_SECRET': config.stripe.webhookSecret,
    'SUPABASE_URL': config.supabase.url,
    'SUPABASE_SERVICE_KEY': config.supabase.serviceKey,
  };

  const missing: string[] = [];
  for (const [key, value] of Object.entries(required)) {
    if (!value) {
      missing.push(key);
    }
  }

  if (missing.length > 0) {
    console.error('❌ Missing required environment variables:', missing.join(', '));
    console.error('Please check your .env file');
    process.exit(1);
  }

  console.log('✅ Configuration validated successfully');
};

// Validate on import
if (process.env.NODE_ENV !== 'test') {
  validateConfig();
}

export type TierName = keyof typeof config.tiers;
export type TierConfig = typeof config.tiers[TierName];
