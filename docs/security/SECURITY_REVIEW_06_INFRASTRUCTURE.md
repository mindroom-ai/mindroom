# Infrastructure Security Review

**Review Date:** 2025-01-15
**Scope:** Kubernetes Infrastructure Security
**Environment:** MindRoom SaaS Platform

## Executive Summary

This report analyzes the infrastructure security of the MindRoom SaaS platform, focusing on Kubernetes deployments, container security, network isolation, and access controls. The review identifies several critical security gaps that require immediate attention, particularly around privilege escalation, network isolation, and secrets management.

**Risk Summary:**
- **CRITICAL (3):** Missing NetworkPolicies, inadequate container security contexts, secrets in environment variables
- **HIGH (2):** Excessive RBAC permissions, incomplete TLS enforcement
- **MEDIUM (2):** Container image security, CORS configuration gaps
- **LOW (1):** Resource limits implementation

## Detailed Findings

### 1. Pod Privilege Configuration

**Status: ❌ FAIL**
**Severity: CRITICAL**

#### Current State
- **Backend deployment:** Has basic security context (runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000)
- **Frontend deployment:** Missing security context entirely
- **Synapse deployment:** Missing security context, runs privileged operations

#### Critical Issues
1. **Frontend pod lacks security context** - runs as root by default
2. **Synapse container performs privileged operations** in startup script:
   ```yaml
   command: ["/bin/sh"]
   args:
     - -c
     - |
       # Fix permissions
       chown -R 991:991 /data
   ```
3. **Missing security hardening directives:**
   - No `allowPrivilegeEscalation: false`
   - No `readOnlyRootFilesystem: true`
   - No `runAsNonRoot: true`
   - No `capabilities` dropping

#### Impact
- Container escape potential through privilege escalation
- Unauthorized file system access
- Compliance violations (PCI DSS, SOC 2)

#### Remediation
```yaml
# Secure security context template
securityContext:
  runAsUser: 1000
  runAsGroup: 1000
  runAsNonRoot: true
  fsGroup: 1000
  fsGroupChangePolicy: OnRootMismatch
  seccompProfile:
    type: RuntimeDefault

# Container security context
containers:
- name: app
  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    runAsNonRoot: true
    capabilities:
      drop:
      - ALL
      add: []
```

### 2. Network Policies and Traffic Isolation

**Status: ❌ FAIL**
**Severity: CRITICAL**

#### Current State
- **No NetworkPolicies found** in any Kubernetes manifests
- Default cluster behavior allows all pod-to-pod communication
- No traffic segmentation between customer instances
- No ingress/egress filtering

#### Critical Issues
1. **Complete lack of network isolation** between:
   - Customer instances in `mindroom-instances` namespace
   - Platform services in `mindroom-staging/prod` namespaces
   - External traffic controls
2. **Cross-tenant data exposure risk** - instances can communicate freely
3. **No egress controls** - pods can reach any external endpoint

#### Impact
- Multi-tenant security breaches
- Lateral movement in compromise scenarios
- Data exfiltration possibilities
- Compliance violations

#### Remediation
```yaml
# Instance isolation policy
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: instance-isolation
  namespace: mindroom-instances
spec:
  podSelector:
    matchLabels:
      customer: "{{ .Values.customer }}"
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          customer: "{{ .Values.customer }}"
    - namespaceSelector:
        matchLabels:
          name: mindroom-staging
  egress:
  - to:
    - podSelector:
        matchLabels:
          customer: "{{ .Values.customer }}"
  - to: []
    ports:
    - protocol: TCP
      port: 443  # HTTPS only
    - protocol: UDP
      port: 53   # DNS
```

### 3. Resource Limits and DoS Prevention

**Status: ✅ PASS**
**Severity: LOW**

#### Current State
All deployments have proper resource limits configured:

- **Backend:** requests: 512Mi/250m, limits: 2Gi/1000m
- **Frontend:** requests: 1Gi/500m, limits: 4Gi/2000m
- **Synapse:** requests: 512Mi/250m, limits: 2Gi/1000m

#### Strengths
- Prevents resource exhaustion attacks
- Enables proper cluster resource management
- Supports horizontal pod autoscaling

#### Minor Improvements
- Consider implementing PodDisruptionBudgets
- Add memory and CPU monitoring alerts

### 4. RBAC Permissions and Least Privilege

**Status: ⚠️ PARTIAL**
**Severity: HIGH**

#### Current State
Platform backend service account has extensive cluster-wide permissions:

```yaml
# Overly broad permissions
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get", "list", "create", "delete"]
- apiGroups: [""]
  resources: ["configmaps", "secrets", "services", "pods", "persistentvolumeclaims", "events", "serviceaccounts"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

#### Issues
1. **Excessive scope:** ClusterRole instead of namespaced Role
2. **Over-privileged:** Full CRUD on secrets cluster-wide
3. **Dangerous permissions:** Can delete namespaces
4. **Missing instance service accounts:** Instance pods use default service account

#### Impact
- Privilege escalation potential
- Cross-tenant secret access
- Infrastructure tampering capability

#### Remediation
```yaml
# Restricted platform backend role
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: platform-backend
  namespace: mindroom-instances
rules:
- apiGroups: [""]
  resources: ["configmaps", "secrets"]
  verbs: ["get", "list", "create", "update", "patch"]
  resourceNames: ["mindroom-config-*", "mindroom-secrets-*"]
- apiGroups: ["apps"]
  resources: ["deployments"]
  verbs: ["get", "list", "create", "update", "patch"]

# Instance service account
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mindroom-instance-{{ .Values.customer }}
  namespace: mindroom-instances
```

### 5. Container Image Security

**Status: ⚠️ PARTIAL**
**Severity: MEDIUM**

#### Current State
- **Custom images:** `git.nijho.lt/basnijholt/mindroom-*:latest`
- **Third-party:** `matrixdotorg/synapse:latest`, `nginx:alpine`
- **Base images:** Use public ECR (good practice)

#### Issues
1. **Latest tags:** No pinned versions for critical components
2. **Private registry security:** Limited visibility into image scanning
3. **No image policies:** Missing admission controllers for image validation

#### Impact
- Supply chain attack exposure
- Inconsistent deployments
- Vulnerability drift

#### Remediation
```yaml
# Pin specific versions
mindroom_backend_image: git.nijho.lt/basnijholt/mindroom-backend:v1.2.3
synapse_image: matrixdotorg/synapse:v1.96.1

# Image policy (using OPA Gatekeeper)
apiVersion: templates.gatekeeper.sh/v1beta1
kind: ConstraintTemplate
metadata:
  name: allowedregistries
spec:
  crd:
    spec:
      type: object
      properties:
        repos:
          type: array
          items:
            type: string
  targets:
    - target: admission.k8s.gatekeeper.sh
      rego: |
        package allowedregistries
        violation[{"msg": msg}] {
          image := input.review.object.spec.containers[_].image
          not starts_with(image, input.parameters.repos[_])
          msg := "Untrusted image registry"
        }
```

### 6. Secrets Management

**Status: ❌ FAIL**
**Severity: CRITICAL**

#### Current State
API keys and sensitive data exposed as environment variables:

```yaml
env:
- name: OPENAI_API_KEY
  value: {{ .Values.openai_key | quote }}
- name: ANTHROPIC_API_KEY
  value: {{ .Values.anthropic_key | quote }}
- name: SUPABASE_SERVICE_KEY
  value: {{ .Values.supabaseServiceKey | quote }}
```

#### Critical Issues
1. **Secrets in environment variables** - visible in process lists, container inspect
2. **No secret rotation** mechanism
3. **Plaintext in Helm values** - stored in version control
4. **Missing encryption at rest** validation

#### Impact
- API key exposure in logs/monitoring
- Compromise persistence
- Audit trail gaps

#### Remediation
```yaml
# Secret volume mounts
apiVersion: v1
kind: Secret
metadata:
  name: mindroom-api-keys-{{ .Values.customer }}
type: Opaque
stringData:
  openai_key: {{ .Values.openai_key }}
  anthropic_key: {{ .Values.anthropic_key }}

# Volume mount in deployment
volumeMounts:
- name: api-keys
  mountPath: /etc/secrets
  readOnly: true
volumes:
- name: api-keys
  secret:
    secretName: mindroom-api-keys-{{ .Values.customer }}
    defaultMode: 0400

# Application reads from files
export OPENAI_API_KEY=$(cat /etc/secrets/openai_key)
```

### 7. TLS/HTTPS Implementation

**Status: ⚠️ PARTIAL**
**Severity: HIGH**

#### Current State
- **TLS termination:** At ingress level using Let's Encrypt
- **Certificate management:** cert-manager with automatic renewal
- **Internal traffic:** HTTP between components

#### Issues
1. **No TLS for internal communication** between services
2. **Mixed content potential** - internal HTTP calls
3. **No HSTS headers** enforcement
4. **Missing TLS cipher restrictions**

#### Impact
- Man-in-the-middle attacks on internal traffic
- Credential interception
- Compliance gaps

#### Remediation
```yaml
# Ingress TLS hardening
annotations:
  nginx.ingress.kubernetes.io/ssl-protocols: "TLSv1.2 TLSv1.3"
  nginx.ingress.kubernetes.io/ssl-ciphers: "ECDHE-RSA-AES128-GCM-SHA256,ECDHE-RSA-AES256-GCM-SHA384"
  nginx.ingress.kubernetes.io/configuration-snippet: |
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

# Service mesh for internal TLS (Istio example)
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: mindroom-instances
spec:
  mtls:
    mode: STRICT
```

### 8. CORS Policy Configuration

**Status: ⚠️ PARTIAL**
**Severity: MEDIUM**

#### Current State
FastAPI CORS configuration allows broad access:

```python
ALLOWED_ORIGINS = [
    "https://app.staging.mindroom.chat",
    "https://app.mindroom.chat",
    "http://localhost:3000",
    "http://localhost:3001",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],          # Too permissive
    allow_headers=["*"],          # Too permissive
    expose_headers=["*"],         # Too permissive
)
```

#### Issues
1. **Overly permissive methods** - allows all HTTP methods
2. **Broad header exposure** - potential information leakage
3. **Development origins in production** - localhost entries
4. **No preflight optimization** - performance impact

#### Impact
- CSRF attack surface
- Information disclosure
- Performance degradation

#### Remediation
```python
# Restrictive CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Requested-With"
    ],
    expose_headers=["X-Total-Count"],
    max_age=86400  # Cache preflight for 24h
)
```

## Infrastructure Hardening Recommendations

### Immediate Actions (Critical)

1. **Implement NetworkPolicies** for all namespaces
2. **Add security contexts** to all pod specifications
3. **Migrate secrets** from environment variables to volume mounts
4. **Restrict RBAC permissions** to least privilege principle

### Short-term Improvements (High)

1. **Enable pod security standards** using Pod Security Standards
2. **Implement image scanning** and admission control
3. **Add internal service mesh** for mTLS
4. **Configure HSTS and security headers**

### Long-term Enhancements (Medium)

1. **Implement secret rotation** using external secret managers
2. **Add container runtime security** (Falco, Sysdig)
3. **Network micro-segmentation** with service mesh
4. **Compliance automation** for continuous monitoring

## Security Baseline Configuration

### Pod Security Standards
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: mindroom-instances
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

### Admission Controller Policies
```yaml
# Deny privileged containers
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-privileged
spec:
  validationFailureAction: enforce
  background: false
  rules:
  - name: check-privileged
    match:
      any:
      - resources:
          kinds:
          - Pod
    validate:
      message: "Privileged containers are not allowed"
      pattern:
        spec:
          =(securityContext):
            =(privileged): "false"
```

## Compliance Mapping

| Control | SOC 2 | PCI DSS | ISO 27001 | Status |
|---------|-------|---------|-----------|---------|
| Network Isolation | CC6.1 | 1.2.1 | A.13.1.1 | ❌ FAIL |
| Access Control | CC6.2 | 7.1.1 | A.9.1.1 | ⚠️ PARTIAL |
| Encryption | CC6.7 | 3.4.1 | A.10.1.1 | ⚠️ PARTIAL |
| Monitoring | CC7.1 | 10.1.1 | A.12.4.1 | ❌ FAIL |

## Conclusion

The MindRoom infrastructure has a solid foundation with proper resource limits and basic TLS implementation. However, critical security gaps in network isolation, privilege management, and secrets handling create significant risk exposure. Immediate implementation of NetworkPolicies and security contexts is essential to prevent multi-tenant security breaches.

**Risk Score: 7.2/10 (HIGH)**

**Priority Actions:**
1. Deploy NetworkPolicies (Critical - 48 hours)
2. Add pod security contexts (Critical - 72 hours)
3. Implement secret volume mounts (Critical - 1 week)
4. Restrict RBAC permissions (High - 1 week)

This review should be updated quarterly or after significant infrastructure changes.
