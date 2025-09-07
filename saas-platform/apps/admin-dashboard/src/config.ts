/// <reference types="vite/client" />

// Configuration for admin dashboard
// All API calls go through our backend - no secrets in frontend!

export const config = {
  // API endpoint - proxied through Vite in dev, relative in prod
  apiUrl: '/api/admin',  // Updated to use admin router in consolidated backend
  // No more secrets in the frontend!
}
