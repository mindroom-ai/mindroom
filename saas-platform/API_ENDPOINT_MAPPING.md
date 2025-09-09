# API Endpoint Mapping: Backend to Frontend

This document maps all backend API endpoints to their corresponding frontend usage locations.

## Summary

- **Total Backend Endpoints**: 32
- **Endpoints Used in Frontend**: 15
- **Unused Endpoints**: 17 (mostly admin and provisioner endpoints)

## 1. Health Check Endpoints

### GET `/health`
- **Backend**: `backend/routes/health.py:12`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Health check for monitoring
- **Note**: Should be used for frontend health monitoring

## 2. Account Management Endpoints

### GET `/my/account`
- **Backend**: `backend/routes/accounts.py:12`
- **Frontend Usage**:
  - `src/lib/api.ts:23` (getAccount function)
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
  - `src/lib/api.ts:32` (setupAccount function)
  - `src/app/dashboard/page.tsx` (imported)
- **Purpose**: Setup free tier account for new user

## 3. Subscription Endpoints

### GET `/my/subscription`
- **Backend**: `backend/routes/subscriptions.py:11`
- **Frontend Usage**:
  - `src/lib/api.ts` (not directly exported)
  - `src/hooks/useSubscription.ts:36` (via apiCall)
- **Purpose**: Get current user's subscription

## 4. Usage Metrics Endpoints

### GET `/my/usage`
- **Backend**: `backend/routes/usage.py:12`
- **Frontend Usage**:
  - `src/lib/api.ts` (not directly exported)
  - `src/hooks/useUsage.ts:38` (via apiCall)
- **Purpose**: Get usage metrics for current user

## 5. Instance Management Endpoints (User-facing)

### GET `/my/instances`
- **Backend**: `backend/routes/instances.py:18`
- **Frontend Usage**:
  - `src/lib/api.ts:42` (listInstances function)
  - `src/app/dashboard/instance/page.tsx` (imported)
  - `src/hooks/useInstance.ts` (imported)
- **Purpose**: List instances for current user

### POST `/my/instances/provision`
- **Backend**: `backend/routes/instances.py:31`
- **Frontend Usage**:
  - `src/lib/api.ts:51` (provisionInstance function)
  - `src/components/dashboard/InstanceCard.tsx` (imported)
- **Purpose**: Provision an instance for the current user

### POST `/my/instances/{instance_id}/start`
- **Backend**: `backend/routes/instances.py:61`
- **Frontend Usage**:
  - `src/lib/api.ts:60` (startInstance function)
  - `src/app/dashboard/instance/page.tsx` (imported)
- **Purpose**: Start user's instance

### POST `/my/instances/{instance_id}/stop`
- **Backend**: `backend/routes/instances.py:80`
- **Frontend Usage**:
  - `src/lib/api.ts:69` (stopInstance function)
  - `src/app/dashboard/instance/page.tsx` (imported)
- **Purpose**: Stop user's instance

### POST `/my/instances/{instance_id}/restart`
- **Backend**: `backend/routes/instances.py:99`
- **Frontend Usage**:
  - `src/lib/api.ts:78` (restartInstance function)
  - `src/app/dashboard/instance/page.tsx` (imported as apiRestartInstance)
  - `src/hooks/useInstance.ts` (imported as apiRestartInstance)
- **Purpose**: Restart user's instance

## 6. Provisioner Endpoints (API Key Protected)

### POST `/system/provision`
- **Backend**: `backend/routes/provisioner.py:31`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Provision a new instance (internal use)

### POST `/system/instances/{instance_id}/start`
- **Backend**: `backend/routes/provisioner.py:182`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Start an instance (internal use)

### POST `/system/instances/{instance_id}/stop`
- **Backend**: `backend/routes/provisioner.py:219`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Stop an instance (internal use)

### POST `/system/instances/{instance_id}/restart`
- **Backend**: `backend/routes/provisioner.py:256`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Restart an instance (internal use)

### DELETE `/system/instances/{instance_id}/uninstall`
- **Backend**: `backend/routes/provisioner.py:293`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Completely uninstall/deprovision an instance

### POST `/system/sync-instances`
- **Backend**: `backend/routes/provisioner.py:352`
- **Frontend Usage**: ❌ **NOT USED** (Internal API)
- **Purpose**: Sync instance states between database and Kubernetes

## 7. Admin Endpoints

### GET `/admin/stats`
- **Backend**: `backend/routes/admin.py:14`
- **Frontend Usage**:
  - `src/app/admin/page.tsx:26` (via apiCall)
- **Purpose**: Get platform statistics for admin dashboard

### POST `/admin/instances/{instance_id}/restart`
- **Backend**: `backend/routes/admin.py:34`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Restart a customer instance (admin action)

### PUT `/admin/accounts/{account_id}/status`
- **Backend**: `backend/routes/admin.py:59`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Update account status (admin action)

### POST `/admin/auth/logout`
- **Backend**: `backend/routes/admin.py:105`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Admin logout placeholder

### GET `/admin/{resource}`
- **Backend**: `backend/routes/admin.py:112` (React Admin generic list)
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Generic list endpoint for React Admin

### GET `/admin/{resource}/{resource_id}`
- **Backend**: `backend/routes/admin.py:151`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Get single record for React Admin

### POST `/admin/{resource}`
- **Backend**: `backend/routes/admin.py:164`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Create record for React Admin

### PUT `/admin/{resource}/{resource_id}`
- **Backend**: `backend/routes/admin.py:176`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Update record for React Admin

### DELETE `/admin/{resource}/{resource_id}`
- **Backend**: `backend/routes/admin.py:189`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Delete record for React Admin

### GET `/admin/metrics/dashboard`
- **Backend**: `backend/routes/admin.py:202`
- **Frontend Usage**: ❌ **NOT USED**
- **Purpose**: Get dashboard metrics for admin panel

## 8. Stripe Integration Endpoints

### POST `/stripe/checkout`
- **Backend**: `backend/routes/stripe_routes.py:19`
- **Frontend Usage**:
  - `src/lib/api.ts:88` (createCheckoutSession function)
  - `src/app/dashboard/billing/upgrade/page.tsx` (imported)
  - `src/app/pricing/page.tsx` (imported)
- **Purpose**: Create Stripe checkout session for subscription

### POST `/stripe/portal`
- **Backend**: `backend/routes/stripe_routes.py:68`
- **Frontend Usage**:
  - `src/lib/api.ts:100` (createPortalSession function)
  - `src/app/dashboard/billing/page.tsx` (imported)
- **Purpose**: Create Stripe customer portal session

## 9. Webhook Endpoints

### POST `/webhooks/stripe`
- **Backend**: `backend/routes/webhooks.py:51`
- **Frontend Usage**: ❌ **NOT USED** (External webhook)
- **Purpose**: Handle Stripe webhook events

## Analysis & Recommendations

### 1. Unused Endpoints (17 total)
- **Health Check** (1): Should be integrated for monitoring
- **Provisioner Internal APIs** (6): Correctly not exposed to frontend
- **Admin CRUD Operations** (9): Not yet implemented in admin panel
- **Admin Instance Restart** (1): Feature not yet added to admin UI

### 2. Missing Frontend Implementations
- Admin panel needs implementation for:
  - Account status management
  - Instance restart functionality
  - CRUD operations for resources
  - Dashboard metrics display
  - Admin logout

### 3. Security Observations
- ✅ Internal provisioner APIs correctly protected with API keys
- ✅ Admin endpoints properly separated from user endpoints
- ✅ User-facing instance operations properly scoped

### 4. Potential Issues
- **Health endpoint** not monitored from frontend
- **Admin features** partially implemented (only stats used)
- **Metrics dashboard** endpoint exists but not used

### 5. API Consistency
- Most endpoints follow RESTful conventions
- Good separation between user-facing and internal APIs
- Consistent authentication approach using Bearer tokens
