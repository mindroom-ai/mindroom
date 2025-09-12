# Security Review 03: Secrets Management

## Executive Summary

**Overall Status: CRITICAL - Multiple severe issues requiring immediate attention**

This security review reveals critical vulnerabilities in secrets management across the MindRoom project. The analysis uncovered hardcoded API keys in version control, default passwords in production configurations, and insufficient secret storage mechanisms.

**Immediate Action Required**: Multiple production secrets are committed to version control and default passwords must be changed before any deployment.

## Checklist Results

### 1. ✅ Scan entire codebase for hardcoded API keys and secrets
**Status: FAIL - Critical hardcoded secrets found**

#### Hardcoded Secrets Found:

**Production API Keys in `.env` file:**
- OpenAI API Key: `sk-proj-XXX`
- Anthropic API Key: `sk-ant-XXX`
- Google API Key: `XXX`
- OpenRouter API Key: `sk-or-v1-XXX`
- Deepseek API Key: `sk-XXX`
- Google OAuth credentials: Client ID and Secret exposed
- Docker Token: `XXX`
- Matrix credentials: Username and password in plaintext

**Credential Files with Real API Keys:**
- `/mindroom_data/credentials/openai_credentials.json`
- `/mindroom_data/credentials/anthropic_credentials.json`
- `/mindroom_data/credentials/google_credentials.json`
- Multiple other service credential files

**Matrix Signing Key:**
- `/docker/synapse/signing.key` contains actual cryptographic key: `ed25519 XXX`

**Default Database Passwords:**
- Synapse DB: `synapse_password` (in shell scripts and Python)
- Platform DB: `changeme` (in docker-compose.platform.yml)
- Platform Redis: `changeme` (in docker-compose.platform.yml)

### 2. ❌ Verify .env files are properly gitignored and never committed
**Status: FAIL - .env file is committed to repository**

**Critical Issue**: The `.env` file containing production API keys is committed to the git repository and publicly visible. While `.env` is listed in `.gitignore` (line 129), the file was committed before the gitignore rule was added.

### 3. ❌ Check that production secrets are stored securely
**Status: FAIL - Secrets in multiple insecure locations**

**Issues Found:**
- Production API keys in committed `.env` file
- Credential JSON files stored in `mindroom_data/credentials/` with full API keys
- Default passwords used in production configurations
- No encryption at rest for stored credentials

### 4. ⚠️ Ensure Kubernetes secrets are properly encrypted at rest
**Status: PARTIAL - Mixed implementation**

**Analysis:**
- Terraform properly marks sensitive variables with `sensitive = true`
- K8s deployment templates use proper environment variable injection: `value: {{ .Values.openai_key | quote }}`
- However, the default `values.yaml` still contains `matrix_admin_password: "changeme"`
- No evidence of encryption at rest configuration for etcd

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

### 7. ❌ Replace all "changeme" default passwords before deployment
**Status: FAIL - Multiple "changeme" passwords found**

**Default Passwords Found:**
- `docker-compose.platform.yml` line 86: `POSTGRES_PASSWORD=${PLATFORM_DB_PASSWORD:-changeme}`
- `docker-compose.platform.yml` line 105: `redis-server --requirepass ${PLATFORM_REDIS_PASSWORD:-changeme}`
- `saas-platform/k8s/instance/values.yaml` line 22: `matrix_admin_password: "changeme"`

### 8. ❌ Implement secure password generation for Matrix user accounts
**Status: FAIL - Static default password**

**Issues:**
- Matrix admin password hardcoded as "changeme" in K8s values
- No password generation mechanism implemented
- Matrix registration shared secret uses the same "changeme" password
- Matrix macaroon and form secrets also use same weak password

### 9. ❌ Verify Matrix registration tokens are properly secured
**Status: FAIL - Weak token generation**

**Issues:**
- Registration shared secret is the hardcoded "changeme" password
- No proper token generation or rotation mechanism
- Tokens stored in plaintext in ConfigMaps

### 10. ❌ Ensure Matrix admin credentials are stored securely
**Status: FAIL - Multiple security issues**

**Issues:**
- Matrix admin password is "changeme" in default configuration
- Matrix signing key committed to repository
- No proper credential management for Matrix admin accounts

## Risk Assessment

### Critical Risks (Immediate Action Required)

1. **Exposed Production API Keys** - Severity: CRITICAL
   - Multiple production API keys committed to version control
   - Keys have financial cost implications (OpenAI, Anthropic billing)
   - Potential for abuse and unauthorized usage

2. **Committed .env File** - Severity: CRITICAL
   - Contains all production secrets in plaintext
   - Publicly visible in git history
   - Includes OAuth credentials for Google integration

3. **Default "changeme" Passwords** - Severity: HIGH
   - Easily guessable credentials in production configurations
   - Matrix admin access compromised
   - Database and cache services vulnerable

### High Risks

4. **Matrix Signing Key Exposure** - Severity: HIGH
   - Cryptographic key committed to repository
   - Compromises Matrix federation security
   - Could allow message spoofing

5. **Credential Files in Data Directory** - Severity: HIGH
   - JSON files containing full API keys
   - No encryption at rest
   - Accessible to anyone with file system access

## Remediation Plan

### Immediate Actions (Within 24 Hours)

1. **Revoke and Rotate All Exposed API Keys**
   ```bash
   # Keys that must be immediately revoked:
   # - OpenAI: sk-proj-XXX...
   # - Anthropic: sk-ant-api03-XXX...
   # - Google: XXX...
   # - OpenRouter: sk-or-v1-XXX...
   # - Docker Token: XXX...
   ```

2. **Remove .env from Git History**
   ```bash
   git filter-branch --force --index-filter \
     'git rm --cached --ignore-unmatch .env' \
     --prune-empty --tag-name-filter cat -- --all
   ```

3. **Replace All Default Passwords**
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

5. **Encrypt Credentials at Rest**
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

The MindRoom project has critical secret management vulnerabilities that require immediate remediation. The presence of production API keys in version control and default passwords in production configurations poses significant security risks.

**Priority Actions:**
1. Immediately revoke all exposed API keys
2. Remove .env file from git history
3. Replace all default passwords
4. Implement proper encrypted secret storage

This review should be followed by implementation of comprehensive secret management practices and regular security audits to prevent similar issues in the future.
