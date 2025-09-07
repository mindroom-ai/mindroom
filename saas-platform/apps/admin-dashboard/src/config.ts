/// <reference types="vite/client" />

// Simple config that works with Vite's built-in env handling
// In dev: uses VITE_ prefixed env vars
// In prod: values are replaced at build time

export const config = {
  supabaseUrl: import.meta.env.VITE_SUPABASE_URL || '',
  supabaseAnonKey: import.meta.env.VITE_SUPABASE_ANON_KEY || '',
  supabaseServiceKey: import.meta.env.VITE_SUPABASE_SERVICE_KEY || '',
  provisionerUrl: import.meta.env.VITE_PROVISIONER_URL || 'http://localhost:8002',
  provisionerApiKey: import.meta.env.VITE_PROVISIONER_API_KEY || '',
  stripeSecretKey: import.meta.env.VITE_STRIPE_SECRET_KEY || '',
}
