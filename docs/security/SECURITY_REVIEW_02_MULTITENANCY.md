# MindRoom Security Review: Multi-Tenancy & Data Isolation

## Executive Summary

This security review focuses on the multi-tenancy and data isolation aspects of the MindRoom SaaS platform. The analysis covers Row Level Security (RLS) policies, API endpoint access controls, frontend-backend communication, service key management, and potential attack vectors that could compromise tenant isolation.

**Overall Assessment (Sept 17, 2025):** Tenant isolation is strong (RLS + ownership checks). Remaining work is operational – automate RLS tests and document retention/audit visibility – before we call this “done.”

## Security Assessment Results

### 1. Users cannot access other customers' accounts data

**Status: PASS (verified 2025-09-17)**

**Analysis:**
- RLS policy: `"Users can view own account" ON accounts FOR SELECT USING (auth.uid() = id OR is_admin())`
- API endpoint protection: All `/my/account/*` endpoints use `verify_user()` dependency
- Account ID validation: Uses authenticated user's `account_id` from JWT token
- Cross-tenant protection: Account queries filtered by `auth.uid() = id`

**Evidence:**
```sql
-- RLS Policy in 000_consolidated_complete_schema.sql (formerly 000_complete_schema.sql):290
CREATE POLICY "Users can view own account" ON accounts
    FOR SELECT USING (auth.uid() = id OR is_admin());
```

```python
# accounts.py:16 - Proper account isolation
@router.get("/my/account")
async def get_current_account(user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    account_id = user["account_id"]  # From authenticated JWT
    account_result = sb.table("accounts").select("*, subscriptions(*, instances(*))").eq("id", account_id).single().execute()
```

**Recommendation:** No immediate action required. Implementation is secure.

---

### 2. Users cannot access other customers' subscriptions

**Status: PASS**

**Analysis:**
- RLS policy: `"Users can view own subscriptions" ON subscriptions FOR SELECT USING (account_id = auth.uid() OR is_admin())`
- All subscription queries filtered by authenticated user's `account_id`
- Proper foreign key relationships: `subscriptions.account_id` → `accounts.id`

**Evidence:**
```sql
-- RLS Policy in 000_consolidated_complete_schema.sql (formerly 000_complete_schema.sql):298
CREATE POLICY "Users can view own subscriptions" ON subscriptions
    FOR SELECT USING (account_id = auth.uid() OR is_admin());
```

**Recommendation:** No immediate action required. Implementation is secure.

---

### 3. Users cannot access other customers' instances

**Status: PASS**

**Analysis:**
- RLS policy with proper cascading: Uses both direct `account_id` and subscription-based access
- Instance operations require ownership verification: `_verify_instance_ownership_and_proxy()`
- Double-verification pattern in critical operations

**Evidence:**
```sql
-- RLS Policy in 000_consolidated_complete_schema.sql (formerly 000_complete_schema.sql):302-307
CREATE POLICY "Users can view own instances" ON instances
    FOR SELECT USING (
        account_id = auth.uid() OR
        subscription_id IN (SELECT id FROM subscriptions WHERE account_id = auth.uid()) OR
        is_admin()
    );
```

```python
# instances.py:197-217 - Ownership verification
async def _verify_instance_ownership_and_proxy(instance_id: int, user: dict, provisioner_func):
    result = (
        sb.table("instances")
        .select("id")
        .eq("instance_id", instance_id)
        .eq("account_id", user["account_id"])  # Double verification
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Instance not found or access denied")
```

**Recommendation:** Excellent implementation. No changes needed.

---

### 4. Usage metrics are properly isolated per account

**Status: PASS**

**Analysis:**
- RLS policy filters usage metrics through subscription ownership chain
- Usage queries require subscription ownership verification
- Proper aggregation respects tenant boundaries

**Evidence:**
```sql
-- RLS Policy in 000_consolidated_complete_schema.sql (formerly 000_complete_schema.sql):310-314
CREATE POLICY "Users can view own usage" ON usage_metrics
    FOR SELECT USING (
        subscription_id IN (SELECT id FROM subscriptions WHERE account_id = auth.uid()) OR
        is_admin()
    );
```

```python
# usage.py:24-31 - Proper isolation
sub_result = sb.table("subscriptions").select("id").eq("account_id", account_id).single().execute()
usage_result = (
    sb.table("usage_metrics")
    .select("*")
    .eq("subscription_id", subscription_id)  # Filtered by user's subscription
    .execute()
)
```

**Recommendation:** No immediate action required.

---

### 5. Webhook events are isolated per account

**Status: FIXED** ✅

**Analysis:**
- ✅ **RLS policy added** for `webhook_events` table (migration 001)
- ✅ Webhook processing validates tenant ownership
- ✅ Service role properly associates events with tenant account_id
- ✅ Cross-tenant access prevented through RLS and application validation

**Fixed Implementation:**
```sql
-- Migration 001_fix_webhook_tenant_isolation.sql
ALTER TABLE webhook_events ADD COLUMN account_id UUID REFERENCES accounts(id);
CREATE POLICY "Users can view own webhook events" ON webhook_events
    FOR SELECT USING (account_id = auth.uid() OR is_admin());
```

**Evidence of Fix:**
```python
# webhooks.py - Now with tenant validation
def handle_subscription_deleted(subscription: dict) -> tuple[bool, str | None]:
    # Verify subscription exists and get account_id
    sub_result = sb.table("subscriptions").select("account_id").eq("subscription_id", subscription["id"]).single().execute()
    if not sub_result.data:
        return False, None
    account_id = sub_result.data["account_id"]
    # Update with tenant validation
    sb.table("subscriptions").update({"status": "cancelled"}).eq("subscription_id", subscription["id"]).eq("account_id", account_id).execute()
    return True, account_id
```

**Remediation Completed:**
1. ✅ Added webhook event tenant association via account_id column
2. ✅ Implemented RLS policy for webhook_events table
3. ✅ Added tenant validation in all webhook handlers

---

### 6. Audit logs cannot be accessed cross-tenant

**Status: PASS**

**Analysis:**
- Strong RLS policy: Only admins can view audit logs
- No user-level access to audit logs prevents cross-tenant leakage
- Admin actions properly logged with account association

**Evidence:**
```sql
-- RLS Policy in 000_consolidated_complete_schema.sql (formerly 000_complete_schema.sql):324-325
CREATE POLICY "Only admins can view audit logs" ON audit_logs
    FOR SELECT USING (is_admin());
```

**Recommendation:** Consider allowing users to view their own audit logs while maintaining admin oversight.

---

### 7. SQL injection cannot bypass RLS policies

**Status: PASS**

**Analysis:**
- ✅ All database queries use Supabase client with parameterized queries
- ✅ No direct SQL string concatenation found
- ✅ User input properly sanitized through Supabase ORM
- ✅ Admin panel uses parameterized queries for search functionality

**Evidence:**
```python
# All queries use parameterized format like:
sb.table("accounts").select("*").eq("id", account_id).execute()
# No raw SQL string concatenation found in codebase
```

**Attack Vectors Tested:**
- Parameter manipulation in API calls
- Search query injection in admin panel
- Instance ID manipulation in URLs

**Recommendation:** Continue using Supabase client for all queries. Avoid raw SQL.

---

### 8. Service role keys are never exposed to client-side code

**Status: PASS**

**Analysis:**
- ✅ Service keys only used in backend configuration
- ✅ Frontend uses only public `SUPABASE_ANON_KEY`
- ✅ Service keys properly configured as environment variables
- ✅ Docker builds use build args, not embedded secrets

**Evidence:**
```typescript
// platform-frontend/src/lib/supabase/client.ts:7
const { supabaseAnonKey } = getRuntimeConfig()
// Only anon key exposed to client
```

```dockerfile
# Dockerfile.platform-frontend:15-22
ARG SUPABASE_URL
ARG SUPABASE_ANON_KEY
ARG PLATFORM_DOMAIN
ENV NODE_ENV=production \\
    SUPABASE_URL=${SUPABASE_URL} \\
    SUPABASE_ANON_KEY=${SUPABASE_ANON_KEY} \\
    PLATFORM_DOMAIN=${PLATFORM_DOMAIN}
# Only public keys in frontend builds
```

**Recommendation:** Excellent implementation. No changes needed.

## Critical Security Findings

### 1. ~~Webhook Events Lack Tenant Isolation~~ ✅ FIXED

**Issue:** The `webhook_events` table had no RLS policies, potentially allowing cross-tenant access to webhook data.

**Impact:**
- ~~Webhook events could be viewed across tenants~~
- ~~No audit trail for webhook processing per tenant~~
- ~~Potential information disclosure~~

**Remediation Applied:**
```sql
-- Migration 001_fix_webhook_tenant_isolation.sql applied
ALTER TABLE webhook_events ADD COLUMN account_id UUID REFERENCES accounts(id);
CREATE POLICY "Users can view own webhook events" ON webhook_events
    FOR SELECT USING (account_id = auth.uid() OR is_admin());
```

**Status:** ✅ Fixed in migrations 001 and updated webhook handlers (now part of `000_consolidated_complete_schema.sql`)

### 2. ~~Payments Table Lacks Tenant Isolation~~ ✅ FIXED

**Issue:** The `payments` table had no account_id column or RLS policies.

**Remediation Applied:**
```sql
-- Migration 002_fix_payments_tenant_isolation.sql applied
ALTER TABLE payments ADD COLUMN account_id UUID REFERENCES accounts(id);
CREATE POLICY "Users can view own payments" ON payments
    FOR SELECT USING (account_id = auth.uid() OR is_admin());
```

**Status:** ✅ Fixed in migration 002 and updated payment handlers (now part of `000_consolidated_complete_schema.sql`)

### 3. Admin Panel Generic Resource Access (MEDIUM PRIORITY)

**Issue:** Admin panel allows generic CRUD operations on any table without specific tenant context validation.

**Impact:**
- Potential for admin to accidentally modify cross-tenant data
- No audit trail for specific admin actions on resources

**Remediation:**
```python
# Add tenant-aware logging in admin routes
@router.put("/admin/{resource}/{resource_id}")
async def admin_update(resource: str, resource_id: str, data: dict) -> dict[str, Any]:
    # Add tenant context logging before any update
    if resource in ["accounts", "subscriptions", "instances"]:
        # Log which tenant's data is being modified
        pass
```

## Edge Cases and Bypass Scenarios Tested

### 1. JWT Token Manipulation
**Test:** Attempting to modify `user_id` in JWT token to access other accounts
**Result:** ❌ **Failed** - Supabase validates JWT signatures server-side
**Status:** Secure

### 2. Instance ID Enumeration
**Test:** Sequential instance ID guessing to access other tenants' instances
**Result:** ❌ **Failed** - Ownership verification blocks unauthorized access
**Status:** Secure

### 3. Admin Privilege Escalation
**Test:** User attempting to set `is_admin = true` on their account
**Result:** ❌ **Failed** - RLS policy prevents self-admin elevation
**Evidence:**
```sql
WITH CHECK (auth.uid() = id AND NOT is_admin) -- Prevents users from making themselves admin
```
**Status:** Secure

### 4. Subscription ID Manipulation
**Test:** Modifying subscription_id in API calls to access other subscriptions
**Result:** ❌ **Failed** - RLS policies enforce account ownership
**Status:** Secure

### 5. Cross-Tenant Resource Access via Admin Routes
**Test:** Non-admin user accessing admin endpoints
**Result:** ❌ **Failed** - `verify_admin()` dependency blocks access
**Status:** Secure

## RLS Policy Analysis

### Strengths
1. **Comprehensive Coverage:** All sensitive tables have appropriate RLS policies
2. **Cascading Security:** Proper foreign key relationships enforce hierarchical access
3. **Admin Separation:** Clear distinction between user and admin access patterns
4. **Defense in Depth:** Multiple layers of validation (RLS + application-level checks)

### Areas for Improvement (Remaining)
1. **Automated RLS policy tests** – add CI coverage to ensure future schema changes don’t regress isolation.
2. **Tenant-facing audit visibility** – optionally expose per-tenant audit logs while keeping admin oversight.
3. **Retention/cleanup documentation** – document how long webhook payloads/audit logs are stored and enforce via jobs.

## Recommendations

### Immediate Actions (HIGH Priority)
1. **Fix webhook events tenant isolation:**
   ```sql
   ALTER TABLE webhook_events ADD COLUMN account_id UUID REFERENCES accounts(id);
   CREATE POLICY "Users can view own webhook events" ON webhook_events FOR SELECT USING (account_id = auth.uid() OR is_admin());
   ```

2. **Add tenant validation in webhook handlers:**
   ```python
   def handle_subscription_deleted(subscription: dict) -> None:
       # Validate subscription belongs to the account processing this webhook
       sb = ensure_supabase()
       # First verify the subscription exists and get account_id
       sub_result = sb.table("subscriptions").select("account_id").eq("subscription_id", subscription["id"]).single().execute()
       if not sub_result.data:
           logger.warning(f"Webhook received for unknown subscription: {subscription['id']}")
           return

       # Then update with proper logging
       sb.table("subscriptions").update({"status": "cancelled"}).eq("subscription_id", subscription["id"]).execute()
   ```

### Medium Priority Actions
3. **Implement automated RLS testing:**
   ```python
   # Test that user A cannot access user B's data
   async def test_cross_tenant_isolation():
       # Create test scenarios for each table
       pass
   ```

4. **Add comprehensive audit logging for admin actions**

5. **Implement rate limiting on authentication endpoints**

### Long-term Enhancements
6. **Consider implementing row-level encryption for sensitive PII**
7. **Add automated security scanning in CI/CD pipeline**
8. **Implement real-time security monitoring**

## Current Outlook (September 17, 2025)

- **Risk:** Low – RLS + app-layer checks enforce tenant boundaries.
- **Next Steps:** Automate RLS tests; add tenant-facing audit access; publish retention policy.
- **Production Readiness:** ✅ Yes, assuming operational follow-ups above are tracked.
