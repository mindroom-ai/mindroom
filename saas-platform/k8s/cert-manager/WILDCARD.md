# Wildcard SSL Certificate Configuration

## Problem Statement

The original SSL configuration created **individual certificates for each customer instance**, which caused:
- **Rate limit issues**: Let's Encrypt allows only 50 certificates per week per domain
- **Scalability problems**: Each instance needed 3 certificates (main, api, matrix)
- **Maximum 16 instances** before hitting rate limits
- **Certificate management overhead**: Hundreds of certificates to manage and renew

## Solution: Wildcard Certificate with DNS-01 Challenge

We implement a **single wildcard certificate** that covers all customer subdomains:
- `*.staging.mindroom.chat` (covers all customer instances)
- `*.api.staging.mindroom.chat` (covers all API endpoints)
- `*.matrix.staging.mindroom.chat` (covers all Matrix servers)

### Benefits
- ✅ **No rate limits**: One certificate for unlimited instances
- ✅ **Simplified management**: Single certificate to renew
- ✅ **Instant SSL**: New instances get SSL immediately
- ✅ **Cost effective**: No per-instance certificate overhead

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Let's Encrypt                         │
└─────────────────┬───────────────────────────────────────┘
                  │ ACME Protocol
                  ▼
┌─────────────────────────────────────────────────────────┐
│                    cert-manager                          │
│  - Manages certificate lifecycle                         │
│  - Handles automatic renewal                            │
└─────────────────┬───────────────────────────────────────┘
                  │ DNS-01 Challenge
                  ▼
┌─────────────────────────────────────────────────────────┐
│              Porkbun Webhook                             │
│  - Creates DNS TXT records for validation               │
│  - Uses Porkbun API                                     │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│           Wildcard Certificate Secret                    │
│  wildcard-staging-mindroom-tls                          │
│  - Contains *.staging.mindroom.chat cert                │
└─────────────────┬───────────────────────────────────────┘
                  │ Used by
                  ▼
┌─────────────────────────────────────────────────────────┐
│              All Customer Ingresses                      │
│  - 6ca9f23a-ingress                                     │
│  - customer2-ingress                                    │
│  - customer3-ingress                                    │
│  - ... (unlimited instances)                            │
└─────────────────────────────────────────────────────────┘
```

## Components

### 1. Porkbun Webhook (`porkbun-webhook.yaml`)
- Deploys webhook pod that interfaces with Porkbun API
- Handles DNS-01 challenge by creating TXT records
- Runs in `cert-manager-webhook-porkbun` namespace

### 2. DNS-01 ClusterIssuers (`cluster-issuer-dns01.yaml`)
- `letsencrypt-prod-dns01`: Production wildcard certificates
- `letsencrypt-staging-dns01`: Staging for testing
- Configured to use Porkbun webhook solver

### 3. Wildcard Certificate (`wildcard-certificate.yaml`)
- Requests wildcard certificate from Let's Encrypt
- Covers all three wildcard domains
- Stored in `wildcard-staging-mindroom-tls` secret

### 4. Updated Ingress Template (`ingress-wildcard.yaml`)
- References shared wildcard certificate
- No cert-manager annotations needed
- All instances use same TLS secret

## Setup Instructions

### Prerequisites
1. **Porkbun API Access**
   - Get API key from https://porkbun.com/account/api
   - Enable API access for `staging.mindroom.chat` domain

2. **cert-manager installed**
   - Should already be installed via Terraform
   - Version 1.13.0 or higher recommended

3. **DNS configured**
   - `*.staging.mindroom.chat` → Cluster IP
   - `*.api.staging.mindroom.chat` → Cluster IP
   - `*.matrix.staging.mindroom.chat` → Cluster IP

### Initial Setup

```bash
# 1. Set Porkbun credentials (or script will prompt)
export PORKBUN_API_KEY="your-api-key"
export PORKBUN_SECRET_KEY="your-secret-key"

# 2. Run setup script
./scripts/setup-wildcard-certificates.sh

# 3. Wait for certificate (2-5 minutes)
kubectl get certificate -n mindroom-instances -w

# 4. Migrate existing instances
./scripts/migrate-to-wildcard.sh

# 5. Update Helm chart for future deployments
cp k8s/instance/templates/ingress-wildcard.yaml k8s/instance/templates/ingress.yaml
```

## DNS-01 Challenge Process

1. **Certificate Request**: cert-manager requests wildcard cert from Let's Encrypt
2. **Challenge Issued**: Let's Encrypt provides DNS challenge token
3. **DNS Record Creation**: Porkbun webhook creates `_acme-challenge.staging.mindroom.chat` TXT record
4. **Validation**: Let's Encrypt verifies DNS record
5. **Certificate Issuance**: Upon successful validation, certificate is issued
6. **Secret Storage**: Certificate stored in Kubernetes secret
7. **Automatic Renewal**: cert-manager renews 30 days before expiry

## Troubleshooting

### Certificate Not Issuing

```bash
# Check certificate status
kubectl describe certificate wildcard-staging-mindroom-cert -n mindroom-instances

# Check active challenges
kubectl get challenges -n mindroom-instances
kubectl describe challenge <challenge-name> -n mindroom-instances

# Check webhook logs
kubectl logs -n cert-manager-webhook-porkbun deployment/cert-manager-webhook-porkbun

# Check cert-manager logs
kubectl logs -n cert-manager deployment/cert-manager
```

### Common Issues

#### 1. Porkbun API Not Enabled
**Symptom**: Challenge fails with "API not enabled for domain"
**Fix**: Enable API access at https://porkbun.com/account/domainsSpeedy

#### 2. Wrong API Credentials
**Symptom**: 401 or 403 errors in webhook logs
**Fix**: Verify API key and secret key are correct

#### 3. DNS Propagation Delay
**Symptom**: Challenge pending for long time
**Fix**: Wait 5-10 minutes for DNS propagation

#### 4. Rate Limits
**Symptom**: "too many certificates already issued"
**Fix**: Use staging issuer for testing

### Manual DNS Verification

```bash
# Check if TXT record was created
dig TXT _acme-challenge.staging.mindroom.chat

# Should return something like:
# _acme-challenge.staging.mindroom.chat. 300 IN TXT "xxxxxxxxxxx"
```

## Migration Guide

### For Existing Instances

```bash
# Automatic migration
./scripts/migrate-to-wildcard.sh

# Or manual for specific instance
kubectl patch ingress 6ca9f23a-ingress -n mindroom-instances --type=json -p='[
  {"op": "replace", "path": "/spec/tls/0/secretName", "value": "wildcard-staging-mindroom-tls"},
  {"op": "remove", "path": "/metadata/annotations/cert-manager.io~1cluster-issuer"}
]'
```

### For New Deployments

Update the Helm chart to use the wildcard ingress template:
```bash
cp k8s/instance/templates/ingress-wildcard.yaml k8s/instance/templates/ingress.yaml
```

## Monitoring

### Certificate Expiry
```bash
# Check certificate expiry
kubectl get certificate wildcard-staging-mindroom-cert -n mindroom-instances

# Output shows READY and EXPIRY
NAME                            READY   SECRET                         AGE
wildcard-staging-mindroom-cert   True    wildcard-staging-mindroom-tls   30d
```

### Automatic Renewal
- cert-manager automatically renews 30 days before expiry
- No manual intervention required
- Monitor cert-manager logs for renewal status

### Health Checks
```bash
# Quick SSL test for any instance
./scripts/check-ssl-status.sh 6ca9f23a

# Test specific URL
curl -I https://6ca9f23a.staging.mindroom.chat
```

## Security Considerations

1. **API Key Security**
   - Store Porkbun API keys in Kubernetes secrets
   - Never commit credentials to git
   - Use RBAC to limit secret access

2. **Certificate Security**
   - Wildcard certificate has broad access
   - Ensure proper RBAC for secret access
   - Monitor certificate usage

3. **DNS Security**
   - Webhook only has access to create TXT records
   - Cannot modify A/CNAME records
   - API access limited to specific domain

## Cost Analysis

### Before (Individual Certificates)
- 3 certificates per instance
- 50 instances = 150 certificates
- High cert-manager overhead
- Complex renewal management

### After (Wildcard Certificate)
- 1 certificate total
- Unlimited instances
- Minimal overhead
- Simple renewal

## Future Improvements

1. **Multi-domain Support**
   - Add production domain (`*.mindroom.chat`)
   - Separate certificates per environment

2. **High Availability**
   - Deploy webhook with multiple replicas
   - Use pod disruption budgets

3. **Monitoring**
   - Prometheus metrics for certificate expiry
   - Alerting for renewal failures

4. **Backup Strategy**
   - Backup certificate secret
   - Disaster recovery plan

## References

- [cert-manager DNS01 Documentation](https://cert-manager.io/docs/configuration/acme/dns01/)
- [Porkbun API Documentation](https://porkbun.com/api/json/v3/documentation)
- [Let's Encrypt Wildcard Certificates](https://letsencrypt.org/docs/faq/#does-let-s-encrypt-issue-wildcard-certificates)
- [Porkbun Webhook GitHub](https://github.com/mdonoughe/porkbun-webhook)
