# SSL Certificate Configuration for MindRoom

## Overview
This directory contains the cert-manager configuration for automatic SSL certificate provisioning using Let's Encrypt.

## Problem Solved
Customer instances at `*.staging.mindroom.chat` were not getting SSL certificates because the ClusterIssuer resource was missing, even though cert-manager was installed.

## Components

### cluster-issuer-prod.yaml
Defines the production ClusterIssuer:
- `letsencrypt-prod`: Production Let's Encrypt certificates (use for real domains)

### cluster-issuer-staging.yaml
Defines the staging ClusterIssuer:
- `letsencrypt-staging`: Staging certificates for testing (higher rate limits)

## Setup Instructions

### 1. Initial Setup (One-time)
```bash
# Run the setup script
./scripts/setup-ssl-certificates.sh
```

### 2. Prerequisites
- cert-manager must be installed (handled by Terraform with `enable_cert_manager = true`)
- DNS records must point to your cluster's ingress IP
- Port 80 must be accessible for HTTP-01 challenge

### 3. DNS Requirements
For SSL certificates to work, ensure these DNS records exist:
```
*.staging.mindroom.chat          A    <your-k8s-ingress-ip>
*.api.staging.mindroom.chat      A    <your-k8s-ingress-ip>
*.matrix.staging.mindroom.chat   A    <your-k8s-ingress-ip>
```

## How It Works

1. **Ingress Creation**: When a new instance is provisioned, an Ingress is created with the annotation `cert-manager.io/cluster-issuer: letsencrypt-prod`

2. **Certificate Request**: cert-manager detects the annotation and creates a Certificate resource

3. **ACME Challenge**: cert-manager initiates an HTTP-01 challenge with Let's Encrypt:
   - Creates a temporary Ingress rule at `/.well-known/acme-challenge/`
   - Let's Encrypt verifies domain ownership by accessing this URL

4. **Certificate Issuance**: Upon successful challenge, Let's Encrypt issues the certificate

5. **Secret Storage**: The certificate is stored as a Kubernetes Secret (e.g., `mindroom-foo-tls`)

6. **Ingress Update**: The Ingress uses the certificate Secret for TLS termination

## Troubleshooting

### Certificate Not Issuing

1. **Check Certificate Status**:
```bash
kubectl get certificates -n mindroom-instances
kubectl describe certificate mindroom-foo-tls -n mindroom-instances
```

2. **Check Challenges**:
```bash
kubectl get challenges -n mindroom-instances
kubectl describe challenge <challenge-name> -n mindroom-instances
```

3. **Common Issues**:
   - **DNS not configured**: Verify `*.staging.mindroom.chat` resolves to cluster IP
   - **Firewall blocking port 80**: HTTP-01 challenge requires port 80 access
   - **Rate limits**: Switch to `letsencrypt-staging` for testing
   - **Wrong email**: Update email in cluster-issuer.yaml

4. **Check cert-manager logs**:
```bash
kubectl logs -n cert-manager deployment/cert-manager
```

### Rate Limits
- **Production**: 50 certificates per week per registered domain
- **Staging**: 30,000 per week (use for testing)

To use staging certificates (for testing):
1. Edit the ingress annotation: `cert-manager.io/cluster-issuer: letsencrypt-staging`
2. Delete existing certificate: `kubectl delete certificate mindroom-foo-tls -n mindroom-instances`
3. Wait for new certificate to be issued

### Manual Certificate Trigger
If a certificate is stuck, you can manually trigger renewal:
```bash
# Delete the certificate (it will be recreated)
kubectl delete certificate mindroom-foo-tls -n mindroom-instances

# Or delete the secret (cert-manager will recreate it)
kubectl delete secret mindroom-foo-tls -n mindroom-instances
```

## Monitoring

Check certificate expiry:
```bash
kubectl get certificates -n mindroom-instances -o wide
```

The `READY` column shows `True` when the certificate is valid and not expired.

## Email Notifications
Let's Encrypt sends expiry notifications to the email configured in `cluster-issuer.yaml`. Update this to receive important notifications about certificate renewals.

## Security Notes
- Certificates are automatically renewed 30 days before expiry
- Private keys are stored as Kubernetes Secrets with restricted access
- Use separate ClusterIssuers for staging and production environments
