# Critical Security Fixes for Production Release

**Created:** 2025-01-16
**Updated:** 2026-03-18 (Post-doc audit)
**Status:** P0 ⚠️ partial | P1.1 ⚠️ partial | P1.2 ⚠️ partial (secrets lifecycle verification outstanding)

> **Audit note (2026-03-18):** The risk score reduction (6.8→5.8) cited in the summary has no scoring methodology or evidence linking specific fixes to numeric changes.
> P2 items (internal TLS, token cache) show no evidence of progress since September 2025.
>
> **Historical planning record:** Status labels below describe tracked-code observations, not verified deployment state.
> Revalidate them against the current source and target environment before using this document operationally.

## Priority System
- **P0**: Legal/Regulatory blockers - Fix IMMEDIATELY
- **P1**: Security blind spots - Fix within 1 week
- **P2**: Pre-production requirements - Fix within 2 weeks

---

## 🚨 P0: Legal & Regulatory Blockers

### 1. PII Encryption & Data Protection
**Status:** ⚠️ PARTIAL
**Files:** Database schema, logging throughout codebase
**Issues RESOLVED:**
- ⚠️ Sensitive data in logs: `log_sanitizer.py` protects its callers, but standard-library loggers can bypass it.
- ✅ GDPR flows: Export/delete/consent endpoints live with tests
- ✅ Soft delete: 7-day grace period implemented
- ⚠️ PII encryption: Application-level encryption & storage-at-rest verification still pending

**Implementation:**
1. ⚠️ Added a backend sanitizer, but global logging coverage remains incomplete
2. ✅ Added GDPR data export endpoint
3. ✅ Implemented soft delete with grace period
4. ✅ Simple, direct implementation following KISS

### 2. Exposed Secrets & API Keys
**Status:** ⚠️ IN PROGRESS
**Files:** `.env`, git history
**Issues RESOLVED:**
- ✅ Git history scan identified 3 keys in docs (DeepSeek, Google, OpenRouter)
- ⚠️ Obsolete helper scripts remain in the repository: do not run `scripts/rotate-api-keys.sh` or `scripts/apply-rotated-keys.sh`, because their Secret names and keys do not match the current charts.
- ⚠️ Pending: Execute rotation and capture evidence (no rotation report on disk)
- ⚠️ Pending: Confirm leaked keys revoked upstream

**Implementation:**
1. ✅ Checked git history for secrets
2. ⚠️ Replace the obsolete helpers with the current Helm, provisioner, or external-secret rotation procedure
3. ⏳ Awaiting actual key rotation (manual step)

---

## 🔴 P1: Security Blind Spots

### 3. Security Monitoring & Alerting
**Status:** ⚠️ PARTIAL
**Issues RESOLVED:**
- ✅ Attack detection: IP-based failure tracking with auto-blocking
- ✅ Auth failure tracking: In-memory with auto-blocking
- ⚠️ Audit logging: Uncached regular-user verification events are recorded; cache-hit, admin, and SSO paths are not comprehensively covered.
- ⚠️ Alerting & dashboards: PrometheusRule manifests exist, but receiver routing, dashboards, and live deployment are unverified.
- ⚠️ Incident response: Playbook + disclosure channels outstanding

**Implementation:**
1. ✅ Simple module-level functions (no classes)
2. ✅ IP blocking after 5 failures in 15 minutes
3. ✅ 30-minute block duration
4. ⏳ Incident response docs (not critical)

### 4. Critical Secrets Management
**Status:** ⚠️ PARTIAL
**Issues RESOLVED:**
- ✅ K8s Secrets implemented with read-only file mounts
- ⚠️ Rotation run + documentation outstanding
- ⚠️ Etcd-at-rest encryption not yet verified

**Implementation:**
1. ✅ Secrets stored in K8s Secret objects (`secret-api-keys.yaml`)
2. ✅ Mounted as files at `/etc/secrets` with 0400 permissions
3. ✅ Application reads via `_get_secret()` function with file fallback
4. ⏳ Verify etcd encryption (low priority, usually enabled by default)

---

## 🟡 P2: Pre-Production Requirements

### 5. Internal Traffic Encryption
**Status:** ⚠️ MEDIUM
**Issues:**
- No mTLS between services

**Fix:**
1. Evaluate if truly needed for initial release
2. Document as post-launch improvement

### 6. Token Security
**Status:** ⚠️ MEDIUM
**Issues:**
- Token cache without invalidation

**Fix:**
1. Add cache invalidation on logout
2. Add token refresh monitoring

---

## Completed Actions

### ✅ Day 1: Critical Logging Fixes
- [x] Removed all console.log with sensitive data
- [x] Added log sanitization (simple regex-based)
- [x] Tested logging doesn't expose PII

### ✅ Day 1: GDPR Basics
- [x] Added data export endpoint
- [x] Added soft delete mechanism
- [x] Created deletion request handler
- [x] Added consent management

### ✅ Day 1: Monitoring Basics
- [x] Added auth failure tracking
- [x] IP-based auto-blocking
- [ ] Audit logging across regular-user cache hits, admin verification, and SSO paths

### ✅ Complete: Infrastructure
- [x] K8s Secrets already implemented with secure file mounts
- [ ] Replace obsolete rotation helpers and document a tested current rotation process
- [x] Define Prometheus metrics + alert-rule manifests for auth/admin events
- [ ] Configure Alertmanager receivers & security dashboards (low priority)

---

## Success Criteria - STATUS
- ⚠️ Sanitization exists for callers using the backend wrapper; global logger coverage is outstanding.
- ✅ GDPR export/delete/consent endpoints functional (tests cover happy paths)
- ⚠️ Uncached regular-user auth failures are tracked with IP-based blocking and audit logging; other auth surfaces remain incomplete.
- ⚠️ Secrets rotation still requires an executed run + evidence
- ⚠️ Comprehensive monitoring/alerting not yet in place

## Risk Reduction Summary
- **Initial Assessment:** Historical review identified high risk without a reproducible numeric methodology.
- **Current Assessment:** Risk remains medium-high after P0/P1.1 hardening because the outstanding controls below are unresolved.
- **Outstanding:** Secrets lifecycle verification, alerting/IR, pod hardening, dependency automation
- **Production Ready:** ❌ No – maintain staging-only access until outstanding items close

## Implementation Philosophy
- **KISS Principle:** Prefer straightforward modules (e.g., `auth_monitor.py`)
- **Pragmatism:** Focus remediation on demonstrated gaps first (admin auth, rate limiting)
- **Iterative Hardening:** Track remaining items openly instead of glossing over gaps
