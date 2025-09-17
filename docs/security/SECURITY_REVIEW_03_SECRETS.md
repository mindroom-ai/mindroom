# Security Review 03: Secrets Management

## Executive Summary

**Overall Status: PARTIAL – K8s Secrets implemented; rotation & encryption verification pending**
**Updated:** September 17, 2025

Defaults in tracked configs have been removed and Helm templates now generate strong secrets when not provided. K8s Secrets are already properly implemented using secure file-based mounts at `/etc/secrets`. The only remaining item is to confirm etcd encryption at rest (usually enabled by default on cloud providers). If any secrets were ever exposed externally (e.g., in past commits or logs), they must be rotated.

## Checklist Results

### 1. ⚠️ Scan repository for hardcoded API keys and secrets
**Status: INCOMPLETE – Helper scripts exist, but a fresh scan/report is still required**

**September 17, 2025 Status:**
- ✅ Manual review caught three keys in docs (DeepSeek, Google, OpenRouter).
- ⚠️ No evidence of a full trufflehog/gitleaks run in repo history.
- ⚠️ No `P0_2_SECRET_ROTATION_REPORT.md` or equivalent artefact present.
- ✅ `.env` ignored; templates populate strong defaults.

**Next actions:** Run a full secret scan (`trufflehog` or `gitleaks`) and archive results; track rotation of any findings.

### 2. ✅ Verify .env files are properly gitignored and never committed
**Status: PASS – .env is gitignored; verify history**

Action: Ensure `.env` and other secret files were never committed; if they were, rotate keys and purge from history in any public mirrors.

### 3. ✅ Check that production secrets are stored securely
**Status: PASS - K8s Secrets properly implemented with file mounts**

**September 17, 2025 Update:**
- ✅ Secrets provided via Kubernetes Secret volumes and `_get_secret()` fallback
- ✅ Secret files mounted read-only (0400)
- ⚠️ Need documented rotation run + etcd encryption verification

### 4. ⚠️ Ensure Kubernetes secrets are properly encrypted at rest
**Status: PARTIAL – Implementation to be confirmed**

**Analysis:**
- Terraform variables marked sensitive as appropriate
- K8s templates accept secrets; defaults in values.yaml are empty, with strong template defaults
- Etcd encryption at rest not yet confirmed on current cluster

### 5. ✅ Verify Docker images don't contain embedded secrets
**Status: PASS - No secrets embedded in Dockerfiles**

**Analysis:**
- Dockerfiles use proper environment variable patterns
- No COPY commands for sensitive files
- Build args properly scoped for public variables only (NEXT_PUBLIC_*)
- Multi-stage builds prevent credential leakage

### 6. ✅ Check that build logs don't expose sensitive information
**Status: PASS - Proper environment variable usage**

**Analysis:**
- Build scripts use environment variable substitution
- Deploy scripts load env vars using python-dotenv with shell format
- No echo or logging of sensitive values in scripts

### 7. ✅ Replace all "changeme" default passwords before deployment
**Status: PASS – Insecure defaults removed (tracked configs)**

Changes:
- `docker-compose.platform.yml`: no default password fallbacks; explicit env required
- `cluster/k8s/instance/values.yaml`: defaults empty; templates generate strong secrets

### 8. ✅ Implement secure password generation for Matrix user accounts
**Status: PASS - Strong defaults when not provided**

**Update:**
- Helm template now defaults `registration_shared_secret`, `macaroon_secret_key`, and `form_secret` to strong random values when not explicitly set

### 9. ⚠️ Verify Matrix registration tokens are properly secured
**Status: PARTIAL - Strong defaults added; rotation pending**

**Update:**
- Strong random defaults added; rotation and secret store integration remain

### 10. ⚠️ Ensure Matrix admin credentials are stored securely
**Status: PARTIAL – Defaults removed; secret store/rotation pending**

Notes:
- Use K8s Secret for admin credentials and signing key; plan rotation

## Risk Assessment

### Critical/High Risks (September 17, 2025)

1. **Secrets lifecycle verification** – Severity: HIGH
   - Helper scripts exist, but no recorded rotation run or provider confirmation.
   - Etcd-at-rest encryption for managed clusters still unverified.

2. **Historical exposure follow-up** – Severity: HIGH
   - Keys previously present in docs; must ensure upstream rotation + purge.
   - Maintain `scripts/clean-git-history.sh`, but verify it has been executed on any public mirrors.

3. **Default credentials** – Severity: RESOLVED
   - Helm/Compose configs now require explicit secrets; keep validation in CI.

## Remediation Plan

### Immediate Actions (as applicable)

1. **Revoke and rotate any previously exposed API keys**
   ```bash
   # Keys that must be immediately revoked:
   # - OpenAI: sk-proj-XXX...
   # - Anthropic: sk-ant-api03-XXX...
   # - Google: XXX...
   # - OpenRouter: sk-or-v1-XXX...
   # - Docker Token: XXX...
   ```

2. **Purge committed secrets from history (if any)**
   ```bash
   git filter-branch --force --index-filter \
     'git rm --cached --ignore-unmatch .env' \
     --prune-empty --tag-name-filter cat -- --all
   ```

3. **Enforce strong passwords (validated)**
   ```bash
   # Generate secure passwords
   openssl rand -base64 32  # For PostgreSQL
   openssl rand -base64 32  # For Redis
   openssl rand -base64 32  # For Matrix admin
   ```

### Short-term Actions (Within 1 Week)

4. **Implement Secure Secret Management**
   ```yaml
   # Use Kubernetes secrets instead of ConfigMaps
   apiVersion: v1
   kind: Secret
   metadata:
     name: matrix-admin-secret
   type: Opaque
   data:
     password: <base64-encoded-secure-password>
   ```

5. **Encrypt Credentials at Rest (confirm etcd encryption)**
   ```python
   from cryptography.fernet import Fernet

   class EncryptedCredentialsManager:
       def __init__(self, key: bytes):
           self.cipher = Fernet(key)

       def store_credential(self, service: str, credential: str):
           encrypted = self.cipher.encrypt(credential.encode())
           # Store encrypted credential
   ```

6. **Implement Proper Matrix Key Management**
   ```bash
   # Generate new signing key securely
   docker exec synapse python -m synapse.app.homeserver \
     --generate-keys --config-path /data/homeserver.yaml
   ```

### Long-term Actions (Within 1 Month)

7. **Integrate with External Secret Management**
   - Consider HashiCorp Vault, AWS Secrets Manager, or Azure Key Vault
   - Implement secret rotation mechanisms
   - Add audit logging for secret access

8. **Implement Zero-Trust Secret Access**
   ```yaml
   # Example RBAC for secret access
   apiVersion: rbac.authorization.k8s.io/v1
   kind: Role
   metadata:
     name: secret-reader
   rules:
   - apiGroups: [""]
     resources: ["secrets"]
     verbs: ["get", "list"]
   ```

## Secure Implementation Examples

### 1. Environment Variable Management
```bash
# .env.example (template only)
OPENAI_API_KEY=your_openai_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here

# Actual .env should never be committed
echo ".env" >> .gitignore
```

### 2. Kubernetes Secret Management
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: api-keys-secret
  namespace: mindroom-instances
type: Opaque
stringData:
  openai-key: "${OPENAI_API_KEY}"
  anthropic-key: "${ANTHROPIC_API_KEY}"
---
# Reference in deployment
env:
- name: OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: api-keys-secret
      key: openai-key
```

### 3. Secure Password Generation
```python
import secrets
import string

def generate_secure_password(length: int = 32) -> str:
    """Generate a cryptographically secure password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(length))
```

### 4. Encrypted Credential Storage
```python
import json
from pathlib import Path
from cryptography.fernet import Fernet

class SecureCredentialsManager:
    def __init__(self, key_file: Path):
        if not key_file.exists():
            key = Fernet.generate_key()
            key_file.write_bytes(key)
        self.cipher = Fernet(key_file.read_bytes())

    def store_credential(self, service: str, data: dict):
        encrypted_data = self.cipher.encrypt(json.dumps(data).encode())
        credential_file = self.base_path / f"{service}.enc"
        credential_file.write_bytes(encrypted_data)
```

## Monitoring and Alerting

### Secret Exposure Detection
```bash
#!/bin/bash
# Pre-commit hook to detect secrets
git diff --cached --name-only | xargs grep -l "sk-\|pk_\|xoxb-\|AIza" && {
  echo "ERROR: Potential secret detected in commit!"
  exit 1
}
```

### Audit Logging
```python
import logging

def log_credential_access(service: str, action: str, user: str):
    logging.info(f"CREDENTIAL_ACCESS: {user} performed {action} on {service}")
```

## Compliance Checklist

- [ ] Remove all committed secrets from git history
- [ ] Revoke and rotate all exposed API keys
- [ ] Replace all default passwords with secure alternatives
- [ ] Implement encrypted credential storage
- [ ] Set up proper Kubernetes secret management
- [ ] Add pre-commit hooks to prevent future secret commits
- [ ] Document secure credential management procedures
- [ ] Train team on secure secret handling practices

## Conclusion

Secret handling has improved substantially (K8s secrets, `_get_secret`, default credential removal). The remaining work centres on **verifying** the lifecycle controls: execute/document key rotation, confirm etcd encryption, and automate secret scanning in CI. Treat these items as pre-launch requirements.

**Immediate Focus:**
1. Run and document the API key rotation scripts; confirm provider revocation.
2. Perform a fresh repo-wide secret scan (trufflehog/gitleaks) and archive results.
3. Verify etcd-at-rest encryption (or enable it) for the target production cluster.

Continue periodic audits to ensure future changes do not reintroduce default credentials or secret leakage.
