export const config = {
  supabaseUrl: import.meta.env.VITE_SUPABASE_URL || '',
  supabaseServiceKey: import.meta.env.VITE_SUPABASE_SERVICE_KEY || '',
  provisionerUrl: import.meta.env.VITE_PROVISIONER_URL || 'http://localhost:8002',
  provisionerApiKey: import.meta.env.VITE_PROVISIONER_API_KEY || '',
  stripeSecretKey: import.meta.env.VITE_STRIPE_SECRET_KEY || '',
}
