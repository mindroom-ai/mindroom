# MindRoom API Security Review - Category 10

**Review Date**: 2024-11-XX
**Scope**: API Security for MindRoom SaaS Platform Backend
**Reviewer**: Claude Code Security Analysis

## Executive Summary

This review examines the API security posture of the MindRoom platform backend, focusing on rate limiting, DoS protection, API versioning, and access controls. The platform consists of 45+ API endpoints across 10 route modules with a clear separation between user-facing and internal system endpoints.

**Updated Status (Sept 17, 2025)**: Rate limiting now covers admin, provisioner, and user endpoints. Remaining gaps: add CAPTCHA/abuse controls for high-risk flows, formalise API versioning strategy, and document API key rotation cadence.

---

## 1. Rate Limiting Implementation (IMPROVED)

### Current Status: **IMPROVED** ✅

**Findings:**
- FastAPI rate limiting integrated via `slowapi`; 429 handler registered
- Per-route limits applied to admin and provisioner endpoints
- **NEW**: Rate limits added to user endpoints (accounts, instances, subscriptions)
  - `/my/instances` - 30/min for listing, 5/min for creation, 10/min for control
  - `/my/account` - 30/min for reading, 5/min for setup
  - `/my/subscription` - 30/min for reading, 5/min for modifications

**High-Risk Endpoints Remaining:**
```python
# /webhooks/stripe                # External webhook - DoS risk (already has 20/min limit)
```

**Security Impact:**
- DoS Attacks – mitigated on admin/provisioner; review remaining surfaces
- Brute Force – apply limits to auth/SSO flows
- Resource Exhaustion – admin/provisioner now protected
- Cost Amplification – reduce by limiting payment/webhook endpoints

### Recommended Implementation

**1. FastAPI Rate Limiting Middleware**
```python
# requirements: slowapi>=0.1.5
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Apply to sensitive endpoints
@router.post("/my/account/setup")
@limiter.limit("5/minute")  # Account creation limit
async def setup_account(request: Request, user: dict = Depends(verify_user)):
    # ... existing code
```

**2. Tiered Rate Limiting Strategy**
```python
RATE_LIMITS = {
    "health": "100/minute",           # High for monitoring
    "auth": "10/minute",              # Prevent brute force
    "payment": "30/hour",             # Payment processing
    "admin": "100/minute",            # Admin operations
    "provisioning": "5/minute",       # Resource-intensive ops
    "webhooks": "1000/minute",        # External services
}
```

---

## 2. Request Size Limits (PASS)

### Current Status: **PASS** ✅

**Findings:**
- Application-level request size limit implemented (1 MiB)
- Ingress limits and upstream defaults can be tuned per endpoint as needed

**Potential Attack Vectors:**
```bash
# Large payload attack examples
curl -X POST /my/account/setup \
  -H "Content-Type: application/json" \
  -d "$(python -c 'print("x" * 50000000)')"  # 50MB payload

curl -X POST /webhooks/stripe \
  -H "Content-Type: application/json" \
  -d "$(cat /dev/zero | head -c 100m)"  # Memory exhaustion
```

### Implementation
```python
# main.py
MAX_REQUEST_BYTES = 1024 * 1024

@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    try:
        length = int(request.headers.get("content-length", "0") or "0")
    except ValueError:
        length = 0
    if length and length > MAX_REQUEST_BYTES:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    return await call_next(request)
```

**Endpoint-Specific Limits (optional)**
```python
ENDPOINT_SIZE_LIMITS = {
    "/my/account/setup": 10240,      # 10KB - account data
    "/stripe/checkout": 4096,        # 4KB - payment request
    "/webhooks/stripe": 65536,       # 64KB - webhook payload
    "/system/provision": 8192,       # 8KB - instance config
}
```

---

## 3. File Upload Security (PASS)

### Current Status: **PASS** ✅

**Findings:**
- **No file upload endpoints detected** in the platform backend
- **Matrix/Synapse**: File uploads handled by Synapse server (separate security boundary)
- **Stripe**: Payment files handled externally by Stripe
- **Instance images**: Managed via Kubernetes/Docker registry (secured separately)

**Security Note**: While no direct file upload vulnerabilities exist in the platform API, future file upload features should implement:
- File type validation
- Size limits
- Virus scanning
- Secure storage with access controls

---

## 4. CAPTCHA Implementation (FAIL)

### Current Status: **FAIL** ❌

**Findings:**
- **No CAPTCHA protection** on any endpoints
- **High-risk operations unprotected**:
  - Account creation (`/my/account/setup`)
  - Password reset workflows (if implemented)
  - Payment processing (`/stripe/checkout`)
  - Admin authentication

**Attack Scenarios:**
```python
# Automated account creation
for i in range(1000):
    requests.post("/my/account/setup",
                  headers={"Authorization": f"Bearer {token}"})

# Automated payment attempts
for i in range(100):
    requests.post("/stripe/checkout",
                  json={"price_id": "price_xxx", "tier": "pro"})
```

### Recommended Implementation

**1. reCAPTCHA Integration**
```python
import httpx

async def verify_recaptcha(token: str) -> bool:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={
                "secret": RECAPTCHA_SECRET_KEY,
                "response": token
            }
        )
        result = response.json()
        return result.get("success", False)

@router.post("/my/account/setup")
async def setup_account(
    request: dict,
    user: dict = Depends(verify_user)
):
    # Verify CAPTCHA for new account setup
    if not await verify_recaptcha(request.get("captcha_token")):
        raise HTTPException(400, "CAPTCHA verification failed")
    # ... continue with setup
```

**2. Risk-Based CAPTCHA Triggers**
- New account registration
- Payment processing above threshold
- Repeated failed authentication attempts
- Admin privilege escalation requests

---

## 5. GraphQL Security (N/A)

### Current Status: **N/A** ✅

**Findings:**
- **No GraphQL endpoints** detected in the platform
- Uses REST API architecture exclusively
- No GraphQL-related dependencies in `pyproject.toml`

**Security Note**: The platform is not vulnerable to GraphQL-specific attacks (query complexity, introspection, etc.) as it does not use GraphQL.

---

## 6. API Versioning Security (PARTIAL)

### Current Status: **PARTIAL** ⚠️

**Findings:**
- **No explicit API versioning** in URL paths (no `/v1/`, `/v2/` patterns)
- **Implicit versioning** through application deployment
- **No deprecation strategy** for endpoint changes
- **Single FastAPI application** serves all endpoints

**Potential Issues:**
```python
# No version-specific endpoint protection
# If /my/account endpoint changes, old clients may break
# No gradual migration or deprecation warnings
```

**Security Implications:**
- Breaking changes could expose security vulnerabilities
- No ability to deprecate insecure endpoint versions
- Client compatibility issues may lead to security bypasses

### Recommended Versioning Strategy

**1. URL-Based Versioning**
```python
# v1 router (current endpoints)
v1_router = APIRouter(prefix="/v1")
v1_router.include_router(accounts.router)
v1_router.include_router(subscriptions.router)

# v2 router (future enhanced security)
v2_router = APIRouter(prefix="/v2")
v2_router.include_router(enhanced_accounts.router)

app.include_router(v1_router)
app.include_router(v2_router)

# Deprecation middleware
@v1_router.middleware("http")
async def deprecation_warning(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-API-Deprecation"] = "v1 deprecated, migrate to v2"
    return response
```

**2. Version-Specific Security Controls**
```python
# Enhanced security for v2
@v2_router.middleware("http")
async def enhanced_security(request: Request, call_next):
    # Additional rate limiting, validation, logging
    return await call_next(request)
```

---

## 7. Internal vs External API Separation (PASS)

### Current Status: **PASS** ✅

**Findings:**
- **Clear separation implemented** between internal and external APIs
- **Internal endpoints**: `/system/*` routes (6 endpoints) with API key protection
- **External endpoints**: `/my/*`, `/admin/*`, `/stripe/*` with JWT authentication
- **Proxy pattern**: Admin endpoints proxy to system endpoints with API key injection

**Security Architecture:**
```python
# Internal system endpoints (API key protected)
/system/provision                 # Instance provisioning
/system/instances/{id}/start      # Instance management
/system/instances/{id}/stop
/system/instances/{id}/restart
/system/instances/{id}/uninstall
/system/sync-instances           # State synchronization

# External endpoints (JWT protected)
/my/account                      # User account access
/my/instances                    # User instance management
/admin/stats                     # Admin dashboard (proxies to system)
```

**Verification:**
```python
# All system endpoints require PROVISIONER_API_KEY
if authorization != f"Bearer {PROVISIONER_API_KEY}":
    raise HTTPException(status_code=401, detail="Unauthorized")

# Admin endpoints proxy with API key injection
return await provisioner_func(instance_id, f"Bearer {PROVISIONER_API_KEY}")
```

**Security Benefits:**
- Internal operations isolated from external access
- API key never exposed to browser/client
- Admin operations properly mediated through proxy pattern
- Clear audit trail for privileged operations

---

## 8. API Key Management and Rotation (PARTIAL)

### Current Status: **PARTIAL** ⚠️

**Findings:**
- **API keys properly configured** via environment variables
- **No hardcoded secrets** detected in source code
- **Secure transmission** using Bearer token authentication
- **Missing rotation mechanism** - no automated key rotation

**Current API Key Usage:**
```python
# Environment-based configuration (secure)
PROVISIONER_API_KEY = os.getenv("PROVISIONER_API_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Secure validation pattern
if authorization != f"Bearer {PROVISIONER_API_KEY}":
    raise HTTPException(status_code=401, detail="Unauthorized")
```

**Security Gaps:**
- No API key expiration or rotation schedule
- No multiple API key support for zero-downtime rotation
- No API key usage monitoring or anomaly detection
- No emergency revocation mechanism

### Recommended API Key Management

**1. Rotation-Capable System**
```python
class APIKeyManager:
    def __init__(self):
        self.active_keys = set([
            os.getenv("PROVISIONER_API_KEY_PRIMARY"),
            os.getenv("PROVISIONER_API_KEY_SECONDARY")
        ])

    def validate_key(self, provided_key: str) -> bool:
        return provided_key in self.active_keys

    def rotate_keys(self):
        # Implement key rotation logic
        # Update Kubernetes secrets
        # Notify dependent services
        pass

key_manager = APIKeyManager()

async def verify_api_key(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing API key")

    key = authorization.replace("Bearer ", "")
    if not key_manager.validate_key(key):
        raise HTTPException(401, "Invalid API key")

    return key
```

**2. Monitoring and Alerting**
```python
# API key usage tracking
async def log_api_key_usage(key: str, endpoint: str, success: bool):
    await logger.info({
        "event": "api_key_usage",
        "key_hash": hashlib.sha256(key.encode()).hexdigest()[:8],
        "endpoint": endpoint,
        "success": success,
        "timestamp": datetime.now().isoformat()
    })

# Anomaly detection
async def detect_anomalies(key_hash: str):
    # Check for unusual usage patterns
    # Geographic anomalies
    # Volume spikes
    # Failed authentication patterns
    pass
```

---

## 9. DoS Vulnerability Assessment

### Attack Vector Analysis

**1. Endpoint-Specific DoS Risks**

| Endpoint | Risk Level | Attack Vector | Impact |
|----------|------------|---------------|---------|
| `/system/provision` | **CRITICAL** | Resource exhaustion through instance creation | $$ cost, cluster overload |
| `/admin/stats` | ~~**HIGH**~~ **RESOLVED** | ~~Database query flooding~~ Rate limited | ~~Database performance degradation~~ |
| `/my/instances` | ~~**HIGH**~~ **RESOLVED** | ~~Kubernetes API flooding~~ Rate limited | ~~Cluster API rate limiting~~ |
| `/webhooks/stripe` | ~~**MEDIUM**~~ **RESOLVED** | ~~Payload flooding~~ Rate limited | ~~Memory exhaustion~~ |
| `/health` | **LOW** | Simple request flooding | Server resource exhaustion |

**2. Resource Exhaustion Vectors**
```bash
# Instance provisioning DoS
for i in {1..100}; do
  curl -X POST /system/provision \
    -H "Authorization: Bearer $API_KEY" \
    -d '{"subscription_id": "test", "tier": "enterprise"}' &
done

# Database flooding via admin stats
for i in {1..1000}; do
  curl /admin/stats -H "Authorization: Bearer $JWT" &
done

# Memory exhaustion via large payloads
curl -X POST /webhooks/stripe \
  -H "Content-Type: application/json" \
  -d "$(python -c 'print("{\"data\": \"" + "x"*10000000 + "\"}")')"
```

**3. Cost Amplification Attacks**
```python
# Expensive Kubernetes operations
POST /system/provision          # Creates cloud resources ($$$)
POST /admin/instances/1/restart # Kubernetes API calls
GET /admin/stats               # Complex database aggregations
```

### DoS Protection Recommendations

**1. Comprehensive Rate Limiting**
```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

RATE_LIMITS = {
    # Prevent resource DoS
    "provision": "2/minute",        # Instance provisioning
    "restart": "10/minute",         # Instance operations
    "stats": "30/minute",           # Admin dashboard

    # Prevent abuse
    "auth": "10/minute",            # Authentication
    "payment": "20/hour",           # Payment processing
    "webhook": "500/minute",        # External webhooks

    # General protection
    "api": "100/minute",            # General API access
}
```

**2. Resource Quotas and Circuit Breakers**
```python
class ResourceLimiter:
    def __init__(self):
        self.concurrent_provisions = 0
        self.max_concurrent = 5

    async def check_provision_capacity(self):
        if self.concurrent_provisions >= self.max_concurrent:
            raise HTTPException(429, "Provisioning capacity exceeded")

    async def acquire_provision_slot(self):
        self.concurrent_provisions += 1

    async def release_provision_slot(self):
        self.concurrent_provisions -= 1
```

---

## 10. Security Testing Results

### Automated Vulnerability Tests

**1. Rate Limit Bypass Tests**
```bash
# Test: Multiple IP rate limit bypass
curl -H "X-Forwarded-For: 1.1.1.1" /health  # 100 requests
curl -H "X-Forwarded-For: 2.2.2.2" /health  # 100 requests
# Result: No rate limiting detected ❌

# Test: User-Agent rotation
for ua in firefox chrome safari; do
  curl -H "User-Agent: $ua" /my/account/setup  # 100 requests each
done
# Result: No rate limiting detected ❌
```

**2. Payload Size Tests**
```bash
# Test: Large JSON payload
curl -X POST /my/account/setup \
  -H "Content-Type: application/json" \
  -d "$(python -c 'print("{\"data\": \"" + "x"*1000000 + "\"}")')"
# Result: Accepted up to nginx limit (100MB) ⚠️

# Test: Memory exhaustion attempt
curl -X POST /webhooks/stripe \
  -H "Content-Type: application/json"  \
  -d "$(cat /dev/zero | head -c 50m)"
# Result: Limited by nginx, but no application-level protection ⚠️
```

**3. API Enumeration Tests**
```bash
# Test: Endpoint discovery
curl /api/v1/          # 404 (no versioning)
curl /docs             # FastAPI docs exposed?
curl /redoc            # ReDoc exposed?
curl /openapi.json     # OpenAPI schema exposed?
# Result: Default FastAPI docs likely exposed ⚠️
```

---

## 11. Remediation Priority Matrix

### Immediate Actions (Week 1)

| Issue | Priority | Effort | Security Impact |
|-------|----------|--------|-----------------|
| Implement rate limiting | **CRITICAL** | High | Prevents DoS attacks |
| Add request size limits | **HIGH** | Medium | Prevents memory exhaustion |
| Disable API documentation in production | **MEDIUM** | Low | Reduces attack surface |

### Short-term Actions (Month 1)

| Issue | Priority | Effort | Security Impact |
|-------|----------|--------|-----------------|
| Implement CAPTCHA for high-risk operations | **HIGH** | Medium | Prevents automation attacks |
| Add API key rotation mechanism | **MEDIUM** | High | Improves credential security |
| Implement API versioning strategy | **MEDIUM** | Medium | Enables security deprecation |

### Long-term Actions (Quarter 1)

| Issue | Priority | Effort | Security Impact |
|-------|----------|--------|-----------------|
| Advanced rate limiting (geographic, behavioral) | **MEDIUM** | High | Enhanced DoS protection |
| API usage monitoring and anomaly detection | **MEDIUM** | High | Threat intelligence |
| Zero-downtime API key rotation | **LOW** | High | Operational security |

---

## 12. Implementation Examples

### Rate Limiting Middleware Implementation

```python
# requirements.txt
slowapi>=0.1.5
redis>=4.0.0

# backend/middleware/rate_limit.py
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import redis

# Redis-backed rate limiter for production
redis_client = redis.Redis(host='localhost', port=6379, db=0)
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri="redis://localhost:6379"
)

# Custom rate limit per endpoint
class EndpointRateLimiter:
    def __init__(self):
        self.limits = {
            "/system/provision": "2/minute",
            "/admin/stats": "30/minute",
            "/my/account/setup": "5/minute",
            "/stripe/checkout": "10/minute",
            "/webhooks/stripe": "500/minute",
        }

    def get_limit(self, endpoint: str) -> str:
        return self.limits.get(endpoint, "100/minute")

rate_limiter = EndpointRateLimiter()

# Apply to FastAPI app
from main import app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Usage in routes
@router.post("/system/provision")
@limiter.limit("2/minute")
async def provision_instance(request: Request, data: dict, ...):
    # Existing implementation
    pass
```

### Request Size Limiting

```python
# backend/middleware/size_limit.py
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_size: int = 1024 * 1024):  # 1MB default
        super().__init__(app)
        self.max_size = max_size
        self.endpoint_limits = {
            "/system/provision": 8192,      # 8KB
            "/my/account/setup": 4096,      # 4KB
            "/stripe/checkout": 4096,       # 4KB
            "/webhooks/stripe": 65536,      # 64KB
        }

    def get_size_limit(self, path: str) -> int:
        return self.endpoint_limits.get(path, self.max_size)

    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            size = int(content_length)
            limit = self.get_size_limit(request.url.path)

            if size > limit:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "Request entity too large",
                        "max_size": limit,
                        "received_size": size
                    }
                )

        return await call_next(request)

# Apply to app
from main import app
app.add_middleware(RequestSizeLimitMiddleware, max_size=1024*1024)
```

### CAPTCHA Integration

```python
# backend/security/captcha.py
import httpx
from backend.config import RECAPTCHA_SECRET_KEY

class CaptchaValidator:
    def __init__(self):
        self.secret_key = RECAPTCHA_SECRET_KEY
        self.verify_url = "https://www.google.com/recaptcha/api/siteverify"

    async def verify(self, token: str, remote_ip: str = None) -> bool:
        if not token or not self.secret_key:
            return False

        data = {
            "secret": self.secret_key,
            "response": token
        }

        if remote_ip:
            data["remoteip"] = remote_ip

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.verify_url, data=data)
                result = response.json()
                return result.get("success", False)
            except Exception:
                return False

captcha = CaptchaValidator()

# Dependency for CAPTCHA-protected endpoints
async def verify_captcha(
    request: Request,
    captcha_token: str = None
) -> bool:
    if not captcha_token:
        raise HTTPException(400, "CAPTCHA token required")

    remote_ip = request.client.host
    if not await captcha.verify(captcha_token, remote_ip):
        raise HTTPException(400, "CAPTCHA verification failed")

    return True

# Usage in high-risk endpoints
@router.post("/my/account/setup")
async def setup_account(
    request: Request,
    user: dict = Depends(verify_user),
    captcha_valid: bool = Depends(verify_captcha)
):
    # Implementation continues...
    pass
```

---

## 13. Monitoring and Alerting

### Security Metrics Dashboard

```python
# backend/monitoring/api_security.py
from prometheus_client import Counter, Histogram, Gauge

# Metrics collection
api_requests_total = Counter('api_requests_total', 'Total API requests', ['endpoint', 'method', 'status'])
api_rate_limit_hits = Counter('api_rate_limit_hits_total', 'Rate limit violations', ['endpoint', 'client_ip'])
api_request_size = Histogram('api_request_size_bytes', 'Request payload size', ['endpoint'])
api_response_time = Histogram('api_response_time_seconds', 'Response time', ['endpoint'])

# Security event tracking
security_events = Counter('security_events_total', 'Security events', ['event_type', 'severity'])

# Alert thresholds
class SecurityMonitor:
    def __init__(self):
        self.rate_limit_threshold = 100    # alerts per hour
        self.large_payload_threshold = 50  # requests > 1MB per hour
        self.error_rate_threshold = 0.05   # 5% error rate

    async def check_rate_limit_abuse(self, client_ip: str, endpoint: str):
        # Check if client exceeds normal usage patterns
        violations = api_rate_limit_hits.labels(endpoint=endpoint, client_ip=client_ip)._value._value

        if violations > self.rate_limit_threshold:
            await self.send_alert(
                "HIGH",
                f"Rate limit abuse detected from {client_ip} on {endpoint}"
            )

    async def check_payload_abuse(self, endpoint: str, size: int):
        if size > 1024 * 1024:  # > 1MB
            security_events.labels(event_type="large_payload", severity="medium").inc()

    async def send_alert(self, severity: str, message: str):
        # Send to monitoring system (Slack, PagerDuty, etc.)
        pass
```

### Alerting Rules

```yaml
# monitoring/alerts.yaml
groups:
- name: api_security
  rules:
  - alert: HighRateLimitViolations
    expr: rate(api_rate_limit_hits_total[5m]) > 10
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: High rate of rate limit violations detected

  - alert: UnusualPayloadSizes
    expr: histogram_quantile(0.95, api_request_size_bytes) > 1048576
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: Unusually large payloads detected

  - alert: ProvisioningDOS
    expr: rate(api_requests_total{endpoint="/system/provision"}[1m]) > 5
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: Potential DoS attack on provisioning endpoint
```

---

## 14. Security Best Practices Checklist

### ✅ Currently Implemented
- [x] JWT-based authentication for user endpoints
- [x] API key authentication for internal endpoints
- [x] Proper separation of internal vs external APIs
- [x] Environment-based secret management
- [x] HTTPS enforcement via ingress configuration
- [x] CORS configuration with specific allowed origins
- [x] Prometheus metrics + alert rules for auth/admin activity

### ❌ Missing Implementation
- [ ] Rate limiting on all endpoints
- [ ] Request size limits at application level
- [ ] CAPTCHA protection for high-risk operations
- [ ] API versioning strategy
- [ ] API key rotation mechanism
- [ ] API documentation access controls
- [ ] Alert routing & dashboards (configure Alertmanager receivers + visualization)

### ⚠️ Partial Implementation
- [ ] Request size controls (nginx-level only)
- [ ] DoS protection (authentication-based only)
- [ ] Security logging (basic application logs only)

---

## 15. Conclusion and Next Steps

### Risk Summary

The MindRoom platform has **moderate to high API security risks** due to the absence of fundamental protective measures:

1. **Critical**: No rate limiting enables DoS attacks and resource exhaustion
2. **High**: Missing CAPTCHA allows automated abuse of sensitive operations
3. **Medium**: Lack of comprehensive request size controls creates memory exhaustion risks
4. **Medium**: No API versioning strategy complicates security updates

### Current Follow-ups

1. Add CAPTCHA/abuse protection for account creation and other high-risk flows.
2. Formalise API versioning/deprecation documentation.
3. Document API key rotation cadence and wire alerts for abnormal API usage (ties into Monitoring review).

**Overall Assessment (Sept 17, 2025):** Medium risk – rate limiting and request-size checks are in place, but abuse-prevention and long-term key rotation strategy must land before a public launch.
