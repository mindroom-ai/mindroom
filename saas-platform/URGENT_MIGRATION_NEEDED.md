# URGENT: Database Migrations Required for Staging

## Problem
The GDPR deletion feature is failing with a 500 error because the `soft_delete_account` function doesn't exist in the staging database.

## Error Details
```
postgrest.exceptions.APIError: {'message': 'Could not find the function public.soft_delete_account(reason, requested_by, target_account_id) in the schema cache'}
```

## Immediate Action Required

You need to apply the missing migrations to your staging Supabase database.

### Option 1: Apply Individual Migrations (if you have some already applied)

Check which migrations are already applied, then apply the missing ones in order:

1. **Check current state**: Run this in Supabase SQL editor to see what exists:
```sql
-- Check if soft_delete functions exist
SELECT proname FROM pg_proc WHERE proname LIKE '%soft_delete%';

-- Check if GDPR columns exist
SELECT column_name FROM information_schema.columns
WHERE table_name = 'accounts'
AND column_name IN ('deleted_at', 'consent_marketing', 'consent_analytics');
```

2. **Apply missing migrations in order**:
   - `001_add_kubernetes_sync_timestamp.sql` (if not applied)
   - `002_fix_payments_tenant_isolation.sql` (if not applied)
   - `003_fix_webhook_tenant_isolation.sql` (if not applied)
   - **`004_soft_delete.sql`** (DEFINITELY missing - this creates the soft_delete_account function)

### Option 2: Fresh Database Setup (if starting fresh)

Use the consolidated migration that includes everything:
```sql
-- Run in Supabase SQL editor
-- File: saas-platform/supabase/migrations/000_consolidated_complete_schema.sql
```

## Files to Apply

The most critical file that's missing is:
- `saas-platform/supabase/migrations/004_soft_delete.sql`

This file contains:
- `soft_delete_account` function (line 35-66)
- `restore_account` function (line 69-85)
- `hard_delete_account` function (line 88-104)
- GDPR consent columns
- Deletion audit table

## How to Apply

1. Go to your Supabase Dashboard for staging
2. Navigate to SQL Editor
3. Copy the contents of `004_soft_delete.sql`
4. Paste and run it
5. The error should be resolved immediately

## Verification

After applying the migration, verify it worked:
```sql
-- This should return 3 rows (soft_delete_account, restore_account, hard_delete_account)
SELECT proname FROM pg_proc WHERE proname LIKE '%_account';
```

## Important Note

The backend has been deployed with code that expects these database functions to exist. Until you apply the migrations, GDPR deletion features will fail with 500 errors.
