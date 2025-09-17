# Infrastructure Security Review

**Review Date:** 2025-09-12
**Scope:** Kubernetes Infrastructure Security
**Environment:** MindRoom SaaS Platform

## Executive Summary

This report analyzes the infrastructure security of the MindRoom SaaS platform, focusing on Kubernetes deployments, container security, network isolation, and access controls. Since the prior review, baseline isolation and RBAC hardening have been implemented; remaining gaps center on secrets lifecycle and internal TLS.

**Risk Summary (Sept 17, 2025):**
- **High:** Internal TLS/mTLS still outstanding
- **Medium:** Platform pods run as root; images use `:latest`; etcd encryption unverified
- **Low:** Resource alerts, PDBs, policy automation

## Detailed Findings

### 1. Pod Privilege Configuration

**Status: ⚠️ PARTIAL**
**Severity: CRITICAL**

#### Current State
- **Backend deployment:** Security context hardened (runAsNonRoot, drop caps, no priv-esc)
- **Frontend deployment:** App container hardened; nginx kept capable of binding :80
- **Synapse deployment:** Startup performs file ownership adjustments (unchanged)

#### Notes
1. **Synapse container performs privileged operations** in startup script:
   ```yaml
   command: ["/bin/sh"]
   args:
     - -c
     - |
       # Fix permissions
       chown -R 991:991 /data
   ```
2. **Backend hardened** with `allowPrivilegeEscalation: false`, `runAsNonRoot: true`, and capability drop
3. **Frontend app container** hardened; nginx left with defaults to serve on port 80

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

**Status: ✅ PASS (baseline isolation in place)**
**Severity: HIGH (for remaining refinements)**

#### Current State
- Per‑instance NetworkPolicy restricts ingress to required ports and limits egress to DNS and HTTPS
- Platform/backend interacts with instances via namespaced Role + RoleBinding; no cluster‑wide wildcard permissions

#### Remaining Gaps
1. Tighten egress as needed for specific external dependencies (e.g., allowlist destinations)
2. Consider service mesh for zero‑trust intra‑cluster enforcement and telemetry

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

**Status: ✅ PASS (backend scoped to namespace)**
**Severity: MEDIUM (further tightening possible)**

#### Current State
- Backend uses a namespaced Role in `mindroom-instances` with a RoleBinding from the platform namespace service account
- No ClusterRole with broad wildcard access

#### Remaining Gaps
- Review verbs for secret/configmap/actions and restrict to minimal set per controller needs
- Add admission policy to block privileged bindings in sensitive namespaces

### 5. Container Image Security

**Status: ⚠️ PARTIAL**
**Severity: MEDIUM**

#### Current State
- **Custom images:** `git.nijho.lt/basnijholt/mindroom-*:latest`
- **Third-party:** `matrixdotorg/synapse:latest`, `nginx:alpine`
- **Base images:** Use public ECR (good practice)

#### Issues
1. **Latest tags:** No pinned versions for critical components (still outstanding)
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

**Status: ✅ PASS (K8s Secrets implemented with file mounts)**
**Severity: LOW**

#### Current State
- Tracked defaults removed; Helm templates generate strong secrets when not provided
- ✅ K8s Secrets properly implemented: mounted as files at `/etc/secrets` with 0400 permissions
- ✅ Application reads secrets via `_get_secret()` function with file fallback
- ✅ More secure than env vars: won't show in `ps`, `/proc/*/environ`, or crash dumps

#### Remaining (Low Priority)
1. Confirm etcd encryption at rest on the cluster (usually enabled by default)
2. Consider External Secrets Operator for cloud provider integration (optional)

### 7. TLS/HTTPS Implementation

**Status: ⚠️ PARTIAL (edge hardened; internal TLS pending)**
**Severity: HIGH**

#### Current State
- TLS termination at ingress with cert‑manager; TLSv1.2/1.3 and restricted ciphers configured
- HSTS enforced via application headers (API) and ingress HSTS annotations
- Internal service‑to‑service traffic remains HTTP

#### Recommended Improvements
```yaml
# Prefer standard HSTS annotations on NGINX ingress
nginx.ingress.kubernetes.io/hsts: "true"
nginx.ingress.kubernetes.io/hsts-max-age: "31536000"
nginx.ingress.kubernetes.io/hsts-include-subdomains: "true"

# Service mesh for internal TLS (example)
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

**Status: ✅ PASS (restricted)**
**Severity: LOW**

#### Current State
- CORS limited to known origins; in production, localhost origins are filtered
- Allowed methods: GET/POST/PUT/DELETE; limited headers; preflight cached (max_age=86400)

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,           # production excludes localhost
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    expose_headers=["X-Total-Count"],
    max_age=86400,
)
```

## Infrastructure Hardening Recommendations

### Immediate Actions (Critical)

1. **Add NetworkPolicies** for platform namespace
2. **Harden platform deployments** (run as non-root, drop caps)
3. **Confirm secrets/etcd encryption**
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
| Network Isolation | CC6.1 | 1.2.1 | A.13.1.1 | ✅ PASS |
| Access Control | CC6.2 | 7.1.1 | A.9.1.1 | ✅ PASS |
| Encryption | CC6.7 | 3.4.1 | A.10.1.1 | ⚠️ PARTIAL |
| Monitoring | CC7.1 | 10.1.1 | A.12.4.1 | ❌ FAIL |

## Conclusion

The MindRoom infrastructure has a solid foundation with proper resource limits and basic TLS implementation. However, critical security gaps in network isolation, privilege management, and secrets handling create significant risk exposure. Immediate implementation of NetworkPolicies and security contexts is essential to prevent multi-tenant security breaches.

**Risk Score: 6.5/10 (HIGH)**

**Priority Actions:**
1. Implement secret volume mounts / External Secrets (High – 1 week)
2. Confirm etcd encryption; plan rotation (High – 1 week)
3. Evaluate internal mTLS (High – spike + rollout)
4. Add policy automation (Kyverno/Gatekeeper) (Medium)

This review should be updated quarterly or after significant infrastructure changes.
