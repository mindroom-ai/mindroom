# URGENT: Database Migrations Required for Staging

## Problem
The GDPR deletion feature is failing with a 500 error because the `soft_delete_account` function doesn't exist in the staging database.

## Error Details
```
postgrest.exceptions.APIError: {'message': 'Could not find the function public.soft_delete_account(reason, requested_by, target_account_id) in the schema cache'}
```

## Immediate Action Required

The safest path is to reset the staging database schema and apply the consolidated migration. We removed the old incremental migrations, so there is one file to run.

### Steps

1. (Optional) Drop all tables or recreate the staging database since there is no data worth preserving.
2. Open the Supabase SQL editor for staging.
3. Copy the contents of `saas-platform/supabase/migrations/000_consolidated_complete_schema.sql`.
4. Paste and execute the script.
5. Re-deploy the backend once the migration completes.

## Verification

After applying the migration, verify it worked:
```sql
-- This should return 3 rows (soft_delete_account, restore_account, hard_delete_account)
SELECT proname FROM pg_proc WHERE proname LIKE '%_account';
```

## Important Note

The backend has been deployed with code that expects these database functions to exist. Until you apply the consolidated migration, GDPR deletion features will fail with 500 errors.
