# Security Review: Data Protection & Privacy

**Review Date**: 2025-01-11
**Updated**: 2025-09-17 (status refresh)
**Reviewer**: Claude Code Security Audit
**Scope**: MindRoom SaaS Platform Data Protection & Privacy Controls

## Executive Summary

This report evaluates the Data Protection & Privacy controls for the MindRoom SaaS platform. The review covers 6 critical areas: data encryption, logging practices, payment data handling, data deletion mechanisms, GDPR compliance, and data retention policies.

### Overall Risk Assessment: **MEDIUM RISK**

**Status Update (September 17, 2025):** GDPR/delete/consent flows and log sanitisation are in place. Remaining blockers: confirm Supabase encryption-at-rest (or add column-level encryption) and publish retention/cleanup documentation.

**Current Highlights**:
- ⚠️ **Encryption-at-rest evidence missing** (confirm Supabase or layer pgcrypto)
- ✅ Frontend/backend logging sanitized (production-safe)
- ✅ GDPR export/delete/consent live with tests
- ⚠️ Retention/cleanup policy documentation pending (cleanup job exists but not described externally)

## Detailed Findings

### 1. PII Encryption at Rest - ❌ **FAIL**

**Status**: FAIL
**Risk Level**: CRITICAL

#### Current State
- **Database**: Uses Supabase with PostgreSQL backend
- **PII Fields Identified**:
  - `accounts.email` - TEXT (unencrypted)
  - `accounts.full_name` - TEXT (unencrypted)
  - `accounts.company_name` - TEXT (unencrypted)
  - `audit_logs.ip_address` - INET (unencrypted)
  - `webhook_events.payload` - JSONB (may contain PII from Stripe)

#### Evidence
```sql
-- From supabase/migrations/000_consolidated_complete_schema.sql (formerly 000_complete_schema.sql)
CREATE TABLE accounts (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT UNIQUE NOT NULL,           -- ❌ Unencrypted PII
    full_name TEXT,                       -- ❌ Unencrypted PII
    company_name TEXT,                    -- ❌ Unencrypted PII
    stripe_customer_id TEXT UNIQUE,
    tier TEXT DEFAULT 'free',
    is_admin BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    details JSONB DEFAULT '{}'::jsonb,
    ip_address INET,                      -- ❌ Unencrypted PII
    success BOOLEAN DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### Remediation Required
1. **Implement column-level encryption** for PII fields:
   ```sql
   -- Add pgcrypto extension
   CREATE EXTENSION IF NOT EXISTS pgcrypto;

   -- Encrypt PII columns
   ALTER TABLE accounts
   ADD COLUMN email_encrypted BYTEA,
   ADD COLUMN full_name_encrypted BYTEA,
   ADD COLUMN company_name_encrypted BYTEA;

   -- Create encrypted insert/update functions
   CREATE OR REPLACE FUNCTION encrypt_pii_data()
   RETURNS TRIGGER AS $$
   BEGIN
       NEW.email_encrypted = pgp_sym_encrypt(NEW.email, current_setting('app.encryption_key'));
       NEW.full_name_encrypted = pgp_sym_encrypt(COALESCE(NEW.full_name, ''), current_setting('app.encryption_key'));
       NEW.company_name_encrypted = pgp_sym_encrypt(COALESCE(NEW.company_name, ''), current_setting('app.encryption_key'));
       RETURN NEW;
   END;
   $$ LANGUAGE plpgsql;
   ```

2. **Configure Supabase encryption at rest** - verify this is enabled in Supabase Pro/Enterprise

3. **Use application-level encryption** for sensitive fields as additional layer

### 2. Sensitive Data Logging - ✅ **RESOLVED**

**Status**: PASS
**Risk Level**: LOW

#### Frontend Logging - FIXED
**All console.log statements replaced with sanitized logger**:

```typescript
// platform-frontend/src/lib/logger.ts - IMPLEMENTED
const isDevelopment = process.env.NODE_ENV === 'development'

export const logger = {
  log: (...args: any[]) => {
    if (isDevelopment) {
      console.log(...args)
    }
  },
  error: (...args: any[]) => {
    if (isDevelopment) {
      console.error(...args)
    }
  },
  warn: (...args: any[]) => {
    if (isDevelopment) {
      console.warn(...args)
    }
  }
}
```

**Result**: Zero logging in production, full logging in development only

#### Backend Logging - FIXED
**Log sanitization implemented**:

```python
# platform-backend/src/backend/utils/log_sanitizer.py - IMPLEMENTED
import re
import os

PATTERNS = {
    "uuid": re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE),
    "email": re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
    "bearer": re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+', re.IGNORECASE),
    "api_key": re.compile(r'\b(api[_-]?key|secret|token|password)["\']?\s*[:=]\s*["\']?[A-Za-z0-9\-._~+/]{20,}', re.IGNORECASE),
}

def sanitize_message(msg: str) -> str:
    if os.getenv("ENVIRONMENT") != "production":
        return msg  # No sanitization in development
    # Redact sensitive patterns
    msg = PATTERNS["uuid"].sub("[UUID]", msg)
    msg = PATTERNS["email"].sub("[EMAIL]", msg)
    msg = PATTERNS["bearer"].sub("Bearer [TOKEN]", msg)
    msg = PATTERNS["api_key"].sub("[REDACTED]", msg)
    return msg
```

**Result**: Automatic redaction of UUIDs, emails, tokens, and API keys in production

#### Remediation Required
1. **Remove all console.log from production frontend**:
   ```typescript
   // Replace with structured logging that filters sensitive data
   const logger = {
     error: (message: string, context?: any) => {
       if (process.env.NODE_ENV === 'development') {
         console.error(message, sanitizeContext(context));
       }
       // Send to logging service with sanitization
     }
   };

   function sanitizeContext(context: any): any {
     if (!context) return context;
     // Remove sensitive fields
     const sanitized = { ...context };
     delete sanitized.email;
     delete sanitized.token;
     delete sanitized.password;
     return sanitized;
   }
   ```

2. **Implement backend log sanitization**:
   ```python
   # Add to backend/config.py
   import re

   class SensitiveDataFilter(logging.Filter):
       def filter(self, record):
           if hasattr(record, 'msg'):
               # Redact email addresses
               record.msg = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
                                 '[EMAIL_REDACTED]', record.msg)
               # Redact UUIDs (account IDs)
               record.msg = re.sub(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
                                 '[UUID_REDACTED]', record.msg)
           return True
   ```

### 3. Credit Card Data Isolation - ✅ **PASS**

**Status**: PASS
**Risk Level**: LOW

#### Current Implementation
**Proper Stripe integration with no credit card data touching servers**:

```python
# platform-backend/src/backend/routes/stripe_routes.py
@router.post("/stripe/checkout", response_model=UrlResponse)
async def create_checkout_session(
    request: CheckoutRequest,
    user: Annotated[dict | None, Depends(verify_user_optional)],
) -> dict[str, Any]:
    # ✅ Only price_id and tier stored, no payment details
    checkout_params = {
        "line_items": [{"price": request.price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": f"{os.getenv('APP_URL', 'https://app.<superdomain>')}/dashboard?success=true&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{os.getenv('APP_URL', 'https://app.<superdomain>')}/pricing?cancelled=true",
        # ...
    }
    session = stripe.checkout.Session.create(**checkout_params)
    return {"url": session.url}  # ✅ Only redirect URL returned
```

**Webhook handling also secure**:
```python
# platform-backend/src/backend/routes/webhooks.py
def handle_payment_succeeded(invoice: dict) -> None:
    sb.table("payments").insert({
        "invoice_id": invoice["id"],           # ✅ Stripe reference only
        "subscription_id": invoice["subscription"],
        "customer_id": invoice["customer"],   # ✅ Stripe customer ID only
        "amount": invoice["amount_paid"] / 100,
        "currency": invoice["currency"],
        "status": "succeeded",
    }).execute()
```

✅ **No improvements needed** - Credit card data properly isolated to Stripe.

### 4. Data Deletion Mechanisms - ✅ **IMPLEMENTED**

**Status**: PASS
**Risk Level**: LOW
**Implementation Date**: September 15, 2025

#### Current State
**Hard deletes with CASCADE - no audit trail**:

```sql
-- From supabase/migrations/000_consolidated_complete_schema.sql (formerly 000_complete_schema.sql)
CREATE TABLE accounts (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,  -- ❌ Hard delete
    -- ...
);

CREATE TABLE subscriptions (
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, -- ❌ Hard delete
    -- ...
);

CREATE TABLE instances (
    account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,          -- ❌ Hard delete
    subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    -- ...
);

CREATE TABLE audit_logs (
    account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,         -- ⚠️ Orphaned logs
    -- ...
);
```

#### Implementation Summary

**Complete data lifecycle management implemented**:
1. ✅ **Soft delete mechanism** - 7-day grace period before hard deletion
2. ✅ **Deletion audit trail** - comprehensive logging for GDPR compliance
3. ✅ **Controlled deletion** - graceful handling with restoration capability
4. ✅ **Data export** - complete data export before deletion available
5. ✅ **Grace period** - 7-day recovery window implemented

**Key Implementation Details**:
- Database migration 004 adds soft delete functionality
- `soft_delete_account()`, `restore_account()`, `hard_delete_account()` functions
- Comprehensive audit logging in `audit_logs` table
- GDPR-compliant deletion via `/my/gdpr/request-deletion` endpoint
- Account restoration via `/my/gdpr/cancel-deletion` endpoint

#### Remediation Required
1. **Implement soft delete pattern**:
   ```sql
   -- Add soft delete columns to all PII tables
   ALTER TABLE accounts ADD COLUMN deleted_at TIMESTAMPTZ NULL;
   ALTER TABLE accounts ADD COLUMN deletion_reason TEXT NULL;
   ALTER TABLE accounts ADD COLUMN deletion_requested_by UUID NULL;

   -- Create soft delete function
   CREATE OR REPLACE FUNCTION soft_delete_account(
       account_id UUID,
       reason TEXT DEFAULT 'user_request',
       requested_by UUID DEFAULT NULL
   ) RETURNS VOID AS $$
   BEGIN
       -- Soft delete
       UPDATE accounts
       SET deleted_at = NOW(),
           deletion_reason = reason,
           deletion_requested_by = requested_by
       WHERE id = account_id;

       -- Record audit trail in audit_logs
       INSERT INTO audit_logs (account_id, action, resource_type, resource_id, details, success)
       VALUES (
           account_id,
           'gdpr_deletion_scheduled',
           'account',
           account_id::text,
           jsonb_build_object('reason', reason, 'requested_by', requested_by),
           TRUE
       );
   END;
   $$ LANGUAGE plpgsql;
   ```

2. **Implement GDPR-compliant deletion API**:
   ```python
   @router.delete("/gdpr/delete-account")
   async def request_account_deletion(
       user: Annotated[dict, Depends(verify_user)],
       reason: str = "user_request"
   ):
       # Export data for user
       user_data = export_user_data(user["account_id"])

       # Log deletion request
       audit_deletion_request(user["account_id"], reason)

      # Soft delete with 7-day retention
       soft_delete_account(user["account_id"], reason, user["account_id"])

       return {"status": "deletion_scheduled", "data_export": user_data}
   ```

### 5. GDPR Compliance - ✅ **IMPLEMENTED**

**Status**: PASS
**Risk Level**: LOW
**Implementation Date**: September 15, 2025

#### Implemented GDPR Mechanisms
**Complete implementation of core GDPR rights**:

1. **Right to be Informed** ✅
   - Data processing purposes disclosed in export endpoint
   - Data retention periods specified (7 years account, 3 years usage, 7 years audit)
   - Third-party processors identified (Stripe, Supabase, Cloud Provider)

2. **Right of Access** ✅
   - Complete data export via `/my/gdpr/export-data` endpoint
   - Machine-readable JSON format with all personal data
   - Includes account data, subscriptions, instances, usage metrics, activity history

3. **Right to Rectification** ✅
   - Profile updates available through standard account endpoints
   - Comprehensive audit trail for data changes in `audit_logs` table
   - Change tracking with timestamps and responsible user

4. **Right to Erasure** ✅
   - Soft delete with 7-day grace period via `/my/gdpr/request-deletion`
   - Confirmation required to prevent accidental deletion
   - Account restoration possible via `/my/gdpr/cancel-deletion`
   - Complete "right to be forgotten" implementation

5. **Right to Data Portability** ✅
   - Complete data export in machine-readable JSON format
   - Includes all user data, relationships, and metadata
   - Structured format suitable for import into other systems

6. **Right to Object** ✅
   - Consent management via `/my/gdpr/consent` endpoint
   - Marketing and analytics opt-out controls implemented
   - Essential services clearly identified and separated
   - No marketing communication controls

#### Remediation Required
1. **Implement GDPR endpoints**:
   ```python
   # Add to backend/routes/gdpr.py
   @router.get("/gdpr/my-data")
   async def export_my_data(user: Annotated[dict, Depends(verify_user)]):
       """Export all user data in machine-readable format."""
       account_data = get_account_data(user["account_id"])
       subscription_data = get_subscription_data(user["account_id"])
       usage_data = get_usage_data(user["account_id"])
       audit_data = get_audit_data(user["account_id"])

       return {
           "export_date": datetime.now(UTC).isoformat(),
           "account": account_data,
           "subscriptions": subscription_data,
           "usage_metrics": usage_data,
           "audit_logs": audit_data,
           "data_processing_purposes": [
               "service_provision",
               "billing",
               "support",
               "legal_compliance"
           ]
       }

   @router.post("/gdpr/request-deletion")
   async def request_data_deletion(
       user: Annotated[dict, Depends(verify_user)],
       confirmation: bool = False
   ):
       """Request account and data deletion under GDPR Article 17."""
       if not confirmation:
           return {"message": "Please confirm deletion by setting confirmation=true"}

      # Schedule deletion with 7-day grace period
       schedule_account_deletion(user["account_id"])

       # Send confirmation email
       send_deletion_confirmation_email(user["email"])

       return {
           "status": "deletion_scheduled",
           "grace_period_days": 30,
           "final_deletion_date": (datetime.now(UTC) + timedelta(days=30)).isoformat()
       }
   ```

2. **Add consent management**:
   ```sql
   CREATE TABLE user_consents (
       id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
       account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
       consent_type TEXT NOT NULL,  -- 'terms', 'privacy', 'marketing', 'analytics'
       granted BOOLEAN NOT NULL,
       granted_at TIMESTAMPTZ NOT NULL,
       withdrawn_at TIMESTAMPTZ,
       ip_address INET,
       user_agent TEXT
   );
   ```

3. **Implement privacy policy and consent flows**:
   ```typescript
   // Frontend consent management
   const ConsentBanner = () => {
     const [consents, setConsents] = useState({
       necessary: true,  // Always required
       analytics: false,
       marketing: false
     });

     const handleAccept = async () => {
       await api.post('/gdpr/consent', {
         consents,
         timestamp: new Date().toISOString(),
         userAgent: navigator.userAgent
       });
     };
   };
   ```

### 6. Data Retention & Cleanup - ✅ **IMPLEMENTED**

**Status**: PASS
**Risk Level**: LOW
**Implementation Date**: September 15, 2025

#### Implementation Summary
**Comprehensive data retention policies implemented**:

1. ✅ **Automated cleanup** - Soft delete with 7-day hard deletion
2. ✅ **Retention periods defined** - 7 years account/audit, 3 years usage metrics
3. ✅ **Controlled storage** - Grace periods and cleanup procedures
4. ✅ **Archival processes** - Audit trail preservation before deletion
5. ✅ **Comprehensive cleanup** - Via `/backend/tasks/cleanup.py` implementation

**Retention Policy Details (from GDPR export)**:
- Account data: 7 years from account closure
- Usage metrics: 3 years from generation
- Audit logs: 7 years from creation
- Payment data: 7 years for tax compliance

```python
# platform-backend/src/backend/deps.py - Only retention control found
_auth_cache = TTLCache(maxsize=100, ttl=300)  # 5 minutes
```

#### Remediation Required
1. **Define data retention policy**:
   ```sql
   -- Create retention policy table
   CREATE TABLE data_retention_policies (
       id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
       data_type TEXT NOT NULL,  -- 'account_data', 'usage_metrics', 'audit_logs', 'webhook_events'
       retention_days INTEGER NOT NULL,
       archive_before_delete BOOLEAN DEFAULT TRUE,
       created_at TIMESTAMPTZ DEFAULT NOW()
   );

   -- Insert default policies
   INSERT INTO data_retention_policies (data_type, retention_days, archive_before_delete) VALUES
   ('account_data', 2555, TRUE),        -- 7 years for regulatory compliance
   ('usage_metrics', 1095, TRUE),       -- 3 years for billing disputes
   ('audit_logs', 2555, TRUE),          -- 7 years for security compliance
   ('webhook_events', 90, FALSE),       -- 90 days for debugging
   ('deleted_accounts', 30, FALSE);     -- 30 days grace period for recovery
   ```

2. **Implement automated cleanup jobs**:
   ```python
   # Add to backend/routes/maintenance.py
   @router.post("/admin/cleanup/run")
   async def run_data_cleanup(admin: Annotated[dict, Depends(verify_admin)]):
       """Run data retention cleanup process."""
       results = {}

       # Clean up old webhook events
       cutoff_date = datetime.now(UTC) - timedelta(days=90)
       webhook_result = supabase.table("webhook_events")\
           .delete()\
           .lt("created_at", cutoff_date.isoformat())\
           .execute()
       results["webhook_events_deleted"] = len(webhook_result.data or [])

       # Clean up old usage metrics (keep 3 years)
       cutoff_date = datetime.now(UTC) - timedelta(days=1095)
       usage_result = supabase.table("usage_metrics")\
           .delete()\
           .lt("created_at", cutoff_date.isoformat())\
           .execute()
       results["usage_metrics_deleted"] = len(usage_result.data or [])

       # Hard delete accounts after 7-day grace period
       cutoff_date = datetime.now(UTC) - timedelta(days=30)
       deleted_accounts = supabase.table("accounts")\
           .select("id")\
           .not_.is_("deleted_at", "null")\
           .lt("deleted_at", cutoff_date.isoformat())\
           .execute()

       for account in deleted_accounts.data or []:
           hard_delete_account(account["id"])

       results["accounts_hard_deleted"] = len(deleted_accounts.data or [])

       return results
   ```

3. **Add cron job for automated cleanup**:
   ```yaml
   # k8s/platform/templates/cronjob-cleanup.yaml
   apiVersion: batch/v1
   kind: CronJob
   metadata:
     name: data-cleanup
   spec:
     schedule: "0 2 * * 0"  # Weekly at 2 AM Sunday
     jobTemplate:
       spec:
         template:
           spec:
             containers:
             - name: cleanup
               image: curlimages/curl
               command:
               - /bin/sh
               - -c
               - |
                 curl -X POST \
                   -H "Authorization: Bearer $ADMIN_TOKEN" \
                   http://platform-backend:8000/admin/cleanup/run
             restartPolicy: OnFailure
   ```

## Risk Assessment Matrix

| Finding | Risk Level | Impact | Likelihood | Priority |
|---------|------------|--------|------------|----------|
| No PII encryption | CRITICAL | High | High | P0 |
| Sensitive data logging | CRITICAL | High | High | P0 |
| No GDPR compliance | CRITICAL | High | Medium | P0 |
| No data retention | HIGH | Medium | High | P1 |
| Hard delete only | HIGH | Medium | Medium | P1 |
| Credit card isolation | LOW | Low | Low | P3 |

## Remediation Roadmap

### Phase 1: Immediate (1-2 weeks)
1. **Remove all console.log from production frontend**
2. **Implement backend log sanitization**
3. **Add basic soft delete for accounts**
4. **Create GDPR data export endpoint**

### Phase 2: Short-term (2-4 weeks)
1. **Implement PII encryption for new data**
2. **Add consent management system**
3. **Create data deletion workflows**
4. **Implement basic retention policies**

### Phase 3: Medium-term (1-2 months)
1. **Migrate existing PII to encrypted columns**
2. **Complete GDPR compliance implementation**
3. **Add automated cleanup jobs**
4. **Implement comprehensive audit logging**

### Phase 4: Long-term (2-3 months)
1. **Add privacy-by-design patterns**
2. **Implement data minimization**
3. **Add data anonymization capabilities**
4. **Complete compliance documentation**

## Code Examples for Immediate Implementation

### 1. Frontend Log Sanitization
```typescript
// src/lib/logger.ts
interface LogContext {
  [key: string]: any;
}

const SENSITIVE_FIELDS = ['email', 'token', 'password', 'api_key', 'secret'];

function sanitizeContext(context: LogContext): LogContext {
  if (!context || typeof context !== 'object') return context;

  const sanitized = { ...context };

  for (const field of SENSITIVE_FIELDS) {
    if (field in sanitized) {
      sanitized[field] = '[REDACTED]';
    }
  }

  return sanitized;
}

export const logger = {
  error: (message: string, context?: LogContext) => {
    const sanitizedContext = sanitizeContext(context || {});

    if (process.env.NODE_ENV === 'development') {
      console.error(message, sanitizedContext);
    }

    // Send to external logging service
    // sendToLoggingService('error', message, sanitizedContext);
  },

  warn: (message: string, context?: LogContext) => {
    const sanitizedContext = sanitizeContext(context || {});

    if (process.env.NODE_ENV === 'development') {
      console.warn(message, sanitizedContext);
    }

    // sendToLoggingService('warn', message, sanitizedContext);
  }
};
```

### 2. Backend GDPR Data Export
```python
# backend/routes/gdpr.py
from fastapi import APIRouter, Depends
from backend.deps import verify_user
from backend.config import supabase
from datetime import datetime, UTC
from typing import Dict, Any

router = APIRouter()

@router.get("/gdpr/export-data")
async def export_user_data(
    user: Annotated[dict, Depends(verify_user)]
) -> Dict[str, Any]:
    """Export all user data for GDPR compliance."""
    account_id = user["account_id"]

    # Get account data
    account = supabase.table("accounts")\
        .select("*")\
        .eq("id", account_id)\
        .single()\
        .execute()

    # Get subscription data
    subscriptions = supabase.table("subscriptions")\
        .select("*")\
        .eq("account_id", account_id)\
        .execute()

    # Get usage metrics
    usage_metrics = supabase.table("usage_metrics")\
        .select("*")\
        .in_("subscription_id", [s["id"] for s in subscriptions.data or []])\
        .execute()

    # Get audit logs (non-sensitive fields only)
    audit_logs = supabase.table("audit_logs")\
        .select("action,resource_type,created_at,success")\
        .eq("account_id", account_id)\
        .execute()

    return {
        "export_date": datetime.now(UTC).isoformat(),
        "account_id": account_id,
        "personal_data": {
            "email": account.data["email"] if account.data else None,
            "full_name": account.data["full_name"] if account.data else None,
            "company_name": account.data["company_name"] if account.data else None,
            "created_at": account.data["created_at"] if account.data else None,
        },
        "subscriptions": subscriptions.data or [],
        "usage_metrics": usage_metrics.data or [],
        "activity_history": audit_logs.data or [],
        "data_processing_purposes": [
            "Service provision and operation",
            "Billing and payment processing",
            "Customer support",
            "Legal compliance",
            "Security and fraud prevention"
        ],
        "data_retention_periods": {
            "account_data": "7 years from account closure",
            "usage_metrics": "3 years from generation",
            "audit_logs": "7 years from creation",
            "payment_data": "Stored by Stripe per their retention policy"
        }
    }
```

### 3. Soft Delete Implementation
```sql
-- Migration: Add soft delete support
ALTER TABLE accounts ADD COLUMN deleted_at TIMESTAMPTZ NULL;
ALTER TABLE accounts ADD COLUMN deletion_reason TEXT NULL;

-- Update RLS policies to exclude soft-deleted accounts
DROP POLICY "Users can view own account" ON accounts;
CREATE POLICY "Users can view own active account" ON accounts
    FOR SELECT USING (auth.uid() = id AND deleted_at IS NULL);

-- Create soft delete function
CREATE OR REPLACE FUNCTION soft_delete_account(
    target_account_id UUID,
    reason TEXT DEFAULT 'user_request'
) RETURNS VOID AS $$
BEGIN
    UPDATE accounts
    SET deleted_at = NOW(),
        deletion_reason = reason,
        updated_at = NOW()
    WHERE id = target_account_id
    AND deleted_at IS NULL;

    -- Log the deletion
    INSERT INTO audit_logs (account_id, action, resource_type, resource_id, details)
    VALUES (target_account_id, 'soft_delete', 'account', target_account_id::text,
            jsonb_build_object('reason', reason, 'deleted_at', NOW()));
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
```

## Compliance Checklist

### Data Protection Regulation Compliance

#### GDPR Requirements
- [ ] Privacy policy implemented and accessible
- [ ] Consent management system deployed
- [ ] Data subject rights endpoints created
- [ ] Data export functionality working
- [ ] Deletion workflow implemented with grace period
- [ ] Breach notification procedures documented
- [ ] Data Protection Impact Assessment completed
- [ ] Lawful basis for processing documented

#### SOC 2 Type II Requirements
- [ ] Data classification policy implemented
- [ ] Encryption controls for PII documented and tested
- [ ] Data retention policy enforced automatically
- [ ] Access controls for sensitive data verified
- [ ] Audit logging for all data access implemented
- [ ] Incident response procedures for data breaches

#### ISO 27001 Requirements
- [ ] Information security management system documented
- [ ] Risk assessment for data processing completed
- [ ] Security controls for data protection implemented
- [ ] Regular security awareness training conducted
- [ ] Vendor risk assessment for Supabase/Stripe completed

## Monitoring and Alerting Recommendations

### Security Monitoring
```python
# Add to monitoring system
SECURITY_ALERTS = {
    "mass_data_access": {
        "description": "User accessing large amounts of data",
        "threshold": "More than 1000 records in 5 minutes",
        "action": "Alert security team"
    },
    "admin_data_access": {
        "description": "Admin accessing user PII",
        "threshold": "Any admin access to accounts table",
        "action": "Log and notify data protection officer"
    },
    "failed_gdpr_requests": {
        "description": "GDPR request processing failures",
        "threshold": "Any failure in data export/deletion",
        "action": "Immediate escalation to legal team"
    }
}
```

### Data Metrics Dashboard
- Daily PII access counts per user/admin
- GDPR request processing times and success rates
- Data retention policy compliance percentages
- Encryption coverage for PII fields
- Log sanitization effectiveness metrics

## Conclusion (September 17, 2025)

Most operational controls are in place (GDPR workflow, log sanitization, soft delete). Production readiness depends on confirming encryption-at-rest and publishing retention/cleanup policies. Track those items alongside monitoring/alerting work.
