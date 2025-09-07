# SSL Certificate Configuration for MindRoom

## Overview
This directory contains the cert-manager configuration for automatic SSL certificate provisioning using Let's Encrypt.

## How It Works

1. **cert-manager Installation**: Terraform installs cert-manager via the kube-hetzner module
2. **ClusterIssuer Configuration**: Terraform applies ClusterIssuers for Let's Encrypt
3. **Automatic Certificate Request**: When an instance is created with the correct annotation, cert-manager automatically requests a certificate
4. **HTTP-01 Challenge**: Let's Encrypt validates domain ownership via HTTP
5. **Certificate Issuance**: SSL certificate is stored as a Kubernetes secret
6. **Automatic Renewal**: Certificates are renewed 30 days before expiry

## Components

### cluster-issuer-prod.yaml
Production Let's Encrypt issuer for real certificates

### cluster-issuer-staging.yaml
Staging Let's Encrypt issuer for testing (higher rate limits)

## Terraform Integration

The `terraform-k8s/cert-manager.tf` module:
- Waits for cert-manager to be ready
- Creates the mindroom-instances namespace
- Applies both ClusterIssuers
- Provides status output

## Instance Configuration

Each instance ingress needs this annotation:
```yaml
cert-manager.io/cluster-issuer: letsencrypt-prod
```

## Troubleshooting

### Check Certificate Status
```bash
kubectl get certificates -n mindroom-instances
kubectl describe certificate <cert-name> -n mindroom-instances
```

### Check Challenges
```bash
kubectl get challenges -n mindroom-instances
```

### Common Issues
- **DNS not configured**: Ensure domain points to cluster IP
- **Rate limits**: Use staging issuer for testing
- **Port 80 blocked**: HTTP-01 challenge requires port 80 access

## Rate Limits
- Production: 50 certificates per week per domain
- Staging: 30,000 per week (use for testing)
