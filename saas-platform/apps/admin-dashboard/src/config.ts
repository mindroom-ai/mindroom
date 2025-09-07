/// <reference types="vite/client" />

// Declare the runtime config type
declare global {
  interface Window {
    ENV_CONFIG?: {
      VITE_SUPABASE_URL?: string;
      VITE_SUPABASE_ANON_KEY?: string;
      VITE_SUPABASE_SERVICE_KEY?: string;
      VITE_PROVISIONER_URL?: string;
      VITE_PROVISIONER_API_KEY?: string;
      VITE_STRIPE_SECRET_KEY?: string;
    }
  }
}

// Helper to get config value with runtime override
const getConfigValue = (key: string, defaultValue = ''): string => {
  // First check runtime config (from config.js)
  if (window.ENV_CONFIG && window.ENV_CONFIG[key as keyof typeof window.ENV_CONFIG]) {
    return window.ENV_CONFIG[key as keyof typeof window.ENV_CONFIG] || defaultValue;
  }
  // Fallback to build-time config
  return import.meta.env[key] || defaultValue;
};

export const config = {
  supabaseUrl: getConfigValue('VITE_SUPABASE_URL'),
  supabaseAnonKey: getConfigValue('VITE_SUPABASE_ANON_KEY'),
  supabaseServiceKey: getConfigValue('VITE_SUPABASE_SERVICE_KEY'),
  provisionerUrl: getConfigValue('VITE_PROVISIONER_URL', 'http://localhost:8002'),
  provisionerApiKey: getConfigValue('VITE_PROVISIONER_API_KEY'),
  stripeSecretKey: getConfigValue('VITE_STRIPE_SECRET_KEY'),
}
