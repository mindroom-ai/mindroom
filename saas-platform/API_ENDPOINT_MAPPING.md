# API Endpoint Mapping: Backend to Frontend

This document maps all backend API endpoints to their corresponding frontend usage locations.

## Summary

- **Total Backend Endpoints**: 35
- **Endpoints Used in Frontend/Admin**: 24
- **Unused Endpoints**: 11 (internal system APIs and admin mutating routes)

## 1. Health Check Endpoints

### GET `/health`
- **Backend**: `backend/routes/health.py`
- **Frontend Usage**:
  - `platform-frontend/src/app/admin/page.tsx` (system health indicator)
- **Purpose**: Health check for monitoring

## 2. Account Management Endpoints

### GET `/my/account`
- **Backend**: `backend/routes/accounts.py:12`
- **Frontend Usage**:
  - `src/lib/api.ts:24` (getAccount function)
  - `src/lib/auth/admin.ts:44` (admin auth check)
- **Purpose**: Get current user's account with subscription and instances

### GET `/my/account/admin-status`
- **Backend**: `backend/routes/accounts.py:38`
- **Frontend Usage**:
  - `src/lib/auth/admin.ts:25` (requireAdmin function)
  - `src/lib/auth/admin.ts:80` (isAdmin function)
- **Purpose**: Check if current user is an admin

### POST `/my/account/setup`
- **Backend**: `backend/routes/accounts.py:53`
- **Frontend Usage**:
  - `src/lib/api.ts:33` (setupAccount function)
  - `src/app/dashboard/page.tsx` (imported)
- **Purpose**: Setup free tier account for new user

## 3. Subscription Endpoints

### GET `/my/subscription`
- **Backend**: `backend/routes/subscriptions.py:11`
- **Frontend Usage**:
  - `src/hooks/useSubscription.ts:36` (via apiCall)
- **Purpose**: Get current user's subscription

## 4. Usage Metrics Endpoints

### GET `/my/usage`
- **Backend**: `backend/routes/usage.py:12`
- **Frontend Usage**:
  - `src/hooks/useUsage.ts:38` (via apiCall with days parameter)
- **Purpose**: Get usage metrics for current user

## 5. Instance Management Endpoints (User-facing)

### GET `/my/instances`
- **Backend**: `backend/routes/instances.py:18`
- **Frontend Usage**:
  - `src/lib/api.ts:43` (listInstances function)
  - `src/app/dashboard/instance/page.tsx` (imported)
  - `src/hooks/useInstance.ts` (imported)
- **Purpose**: List instances for current user

### POST `/my/instances/provision`
- **Backend**: `backend/routes/instances.py:31`
- **Frontend Usage**:
  - `src/lib/api.ts:52` (provisionInstance function)
  - `src/components/dashboard/InstanceCard.tsx` (imported)
- **Purpose**: Provision an instance for the current user

### POST `/my/instances/{instance_id}/start`
- **Backend**: `backend/routes/instances.py:61`
- **Frontend Usage**:
  - `src/lib/api.ts:61` (startInstance function)
  - `src/app/dashboard/instance/page.tsx` (imported)
- **Purpose**: Start user's instance

### POST `/my/instances/{instance_id}/stop`
- **Backend**: `backend/routes/instances.py:80`
- **Frontend Usage**:
  - `src/lib/api.ts:70` (stopInstance function)
  - `src/app/dashboard/instance/page.tsx` (imported)
- **Purpose**: Stop user's instance

### POST `/my/instances/{instance_id}/restart`
- **Backend**: `backend/routes/instances.py:99`
- **Frontend Usage**:
  - `src/lib/api.ts:79` (restartInstance function)
  - `src/app/dashboard/instance/page.tsx` (imported as apiRestartInstance)
  - `src/hooks/useInstance.ts` (imported as apiRestartInstance)
- **Purpose**: Restart user's instance

## 6. System/Provisioner Endpoints (API Key Protected)

### POST `/system/provision`
- **Backend**: `backend/routes/provisioner.py:37`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Provision a new instance (internal use)

### POST `/system/instances/{instance_id}/start`
- **Backend**: `backend/routes/provisioner.py:189`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Start an instance (internal use)

### POST `/system/instances/{instance_id}/stop`
- **Backend**: `backend/routes/provisioner.py:224`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Stop an instance (internal use)

### POST `/system/instances/{instance_id}/restart`
- **Backend**: `backend/routes/provisioner.py:259`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Restart an instance (internal use)

### DELETE `/system/instances/{instance_id}/uninstall`
- **Backend**: `backend/routes/provisioner.py:294`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Completely uninstall/deprovision an instance

### POST `/system/sync-instances`
- **Backend**: `backend/routes/provisioner.py:339`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Sync instance states between database and Kubernetes

## 7. Admin Endpoints

### GET `/admin/stats`
- **Backend**: `backend/routes/admin.py:20`
- **Frontend Usage**:
  - `src/app/admin/page.tsx:26` (via apiCall)
- **Purpose**: Get platform statistics for admin dashboard

### POST `/admin/instances/{instance_id}/start`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**:
  - `platform-frontend/src/components/admin/InstanceActions.tsx` (Start)
- **Purpose**: Admin start any instance (proxies to provisioner)

### POST `/admin/instances/{instance_id}/stop`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**:
  - `platform-frontend/src/components/admin/InstanceActions.tsx` (Stop)
- **Purpose**: Admin stop any instance (proxies to provisioner)

### POST `/admin/instances/{instance_id}/restart`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**:
  - `platform-frontend/src/components/admin/InstanceActions.tsx` (Restart)
- **Purpose**: Admin restart any instance (proxies to provisioner)

### DELETE `/admin/instances/{instance_id}/uninstall`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**:
  - `platform-frontend/src/components/admin/InstanceActions.tsx` (Uninstall)
- **Purpose**: Admin uninstall any instance (proxies to provisioner)

### PUT `/admin/accounts/{account_id}/status`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**:
  - `platform-frontend/src/app/admin/accounts/page.tsx` (inline status update control)
- **Purpose**: Update account status (active, suspended, etc)

### POST `/admin/auth/logout`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Admin logout placeholder

### GET `/admin/{resource}`
- **Backend**: `backend/routes/admin.py` (generic list)
- **Frontend Usage**:
  - `platform-frontend/src/app/admin/accounts/page.tsx` → `/admin/accounts`
  - `platform-frontend/src/app/admin/subscriptions/page.tsx` → `/admin/subscriptions`
  - `platform-frontend/src/app/admin/instances/page.tsx` → `/admin/instances`
  - `platform-frontend/src/app/admin/audit-logs/page.tsx` → `/admin/audit_logs`
  - `platform-frontend/src/app/admin/usage/page.tsx` → `/admin/usage_metrics`
- **Purpose**: Generic list endpoint for admin resources

### GET `/admin/{resource}/{resource_id}`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Get single record for admin/React Admin

### POST `/admin/{resource}`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Create record for admin/React Admin

### PUT `/admin/{resource}/{resource_id}`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Update record for admin/React Admin

### DELETE `/admin/{resource}/{resource_id}`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Delete record for admin/React Admin

### GET `/admin/metrics/dashboard`
- **Backend**: `backend/routes/admin.py`
- **Frontend Usage**:
  - `platform-frontend/src/app/admin/page.tsx` (API-backed metrics cards)
- **Purpose**: Get dashboard metrics for admin panel

## 8. Stripe Integration Endpoints

### POST `/stripe/checkout`
- **Backend**: `backend/routes/stripe_routes.py:19`
- **Frontend Usage**:
  - `src/lib/api.ts:89` (createCheckoutSession function)
  - `src/app/dashboard/billing/upgrade/page.tsx` (imported)
  - `src/app/pricing/page.tsx` (imported)
- **Purpose**: Create Stripe checkout session for subscription

### POST `/stripe/portal`
- **Backend**: `backend/routes/stripe_routes.py:68`
- **Frontend Usage**:
  - `src/lib/api.ts:101` (createPortalSession function)
  - `src/app/dashboard/billing/page.tsx` (imported)
- **Purpose**: Create Stripe customer portal session

## 9. Webhook Endpoints

### POST `/webhooks/stripe`
- **Backend**: `backend/routes/webhooks.py:51`
- **Frontend Usage**: ❌ **NOT USED** (External webhook from Stripe)
- **Purpose**: Handle Stripe webhook events

## 10. SSO Cookie Endpoints

### POST `/my/sso-cookie`
- **Backend**: `backend/routes/sso.py`
- **Frontend Usage**:
  - `platform-frontend/src/lib/api.ts` (setSsoCookie)
- **Purpose**: Set SSO cookie for seamless instance access across subdomains

### DELETE `/my/sso-cookie`
- **Backend**: `backend/routes/sso.py`
- **Frontend Usage**:
  - `platform-frontend/src/lib/api.ts` (clearSsoCookie)
- **Purpose**: Clear SSO cookie on logout

## Analysis & Recommendations

### 1. Endpoint Categories After Renaming
- **`/my/*`**: 8 endpoints - User-scoped operations requiring JWT authentication
- **`/system/*`**: 6 endpoints - Internal provisioner operations requiring API key
- **`/admin/*`**: 13 endpoints - Admin operations requiring admin privileges
- **`/stripe/*`**: 2 endpoints - Stripe integration
- **`/webhooks/*`**: 1 endpoint - External webhooks
- **`/health`**: 1 endpoint - Health check

### 2. Key Architectural Patterns
- **User isolation**: `/my/*` endpoints verify ownership before operations
- **Admin proxy pattern**: Admin endpoints proxy to system endpoints with API key
- **Internal APIs**: `/system/*` endpoints not exposed to frontend
- **Clear separation**: No ambiguity between user, admin, and system operations

### 3. Unused But Important Endpoints
- **Admin instance control** (4 endpoints): Ready for admin panel implementation
- **React Admin CRUD** (6 endpoints): Ready for React Admin integration
- **Admin metrics**: Dashboard endpoint ready for admin analytics

### 4. Frontend Integration Status
- ✅ All user operations properly integrated
- ✅ Basic admin stats integrated
- ⚠️ Admin instance control not yet integrated in UI
- ⚠️ React Admin not yet implemented
- ✅ Stripe integration complete

### 5. Security Observations
- ✅ User endpoints enforce ownership checks
- ✅ System endpoints protected with API key
- ✅ Admin endpoints require admin verification
- ✅ Admin endpoints proxy to system endpoints (API key never exposed to browser)
- ✅ Clear separation of concerns

### 6. Recommendations
1. **Implement admin panel features** to use the admin instance control endpoints
2. **Add health check monitoring** in frontend
3. **Consider implementing React Admin** for resource management
4. **Add frontend integration** for admin metrics dashboard
