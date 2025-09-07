/// <reference types="vite/client" />

// Configuration for admin dashboard
// All API calls go through our backend - no secrets in frontend!

export const config = {
  // API endpoint - proxied through Vite in dev, relative in prod
  apiUrl: '/api',
  // No more secrets in the frontend!
}
