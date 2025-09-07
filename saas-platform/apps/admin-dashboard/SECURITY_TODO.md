# CRITICAL SECURITY ISSUE: Admin Dashboard

## Problem
The admin dashboard currently exposes the Supabase service key directly in the browser JavaScript bundle. This is a critical security vulnerability as the service key provides full database access.

## Current Implementation (INSECURE)
```javascript
// This runs in the browser - anyone can see it!
const supabase = createClient(config.supabaseUrl, config.supabaseServiceKey)
```

## Required Solution
Convert the admin dashboard to a proper client-server architecture:

### Option 1: Add Backend API Layer
1. Create an Express/Fastify backend service
2. Backend holds the service key securely
3. Frontend authenticates with backend
4. Backend proxies requests to Supabase with service key
5. Deploy as Node.js container (not static nginx)

### Option 2: Use Row Level Security
1. Remove service key from frontend entirely
2. Use Supabase Auth for admin users
3. Implement RLS policies for admin access
4. Use anon key in frontend (safe to expose)

### Option 3: Next.js Full-Stack
1. Migrate admin dashboard to Next.js
2. Use API routes for sensitive operations
3. Keep service key server-side only

## Temporary Mitigation
- Ensure admin dashboard is NOT publicly accessible
- Use network policies to restrict access
- Add authentication proxy in front of admin dashboard

## Impact
- **Severity**: CRITICAL
- **Risk**: Full database access if key is exposed
- **Current Status**: Vulnerable in production

## Action Items
- [ ] Choose architecture approach
- [ ] Implement backend service
- [ ] Remove service key from frontend
- [ ] Add proper authentication
- [ ] Update deployment configuration
- [ ] Security audit after fix
