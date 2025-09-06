# Domain Configuration Guide

## Production Domain Structure

### Platform Services
Fixed subdomains for platform infrastructure:
- `app.mindroom.chat` → Customer Portal (customers login here)
- `admin.mindroom.chat` → Admin Dashboard (internal admin)
- `api.mindroom.chat` → Provisioning API
- `webhooks.mindroom.chat/stripe` → Stripe webhook endpoint

### Customer Instances
Dynamic subdomains per customer:
- `{customer}.mindroom.chat` → Customer's MindRoom UI
- `{customer}.api.mindroom.chat` → Customer's backend API
- `{customer}.matrix.mindroom.chat` → Customer's Matrix server

Example for customer "acme":
- `acme.mindroom.chat`
- `acme.api.mindroom.chat`
- `acme.matrix.mindroom.chat`

## DNS Setup

### For Production

Add these DNS records to your domain:

```
# Platform services
app.mindroom.chat        A     <your-k8s-ingress-ip>
admin.mindroom.chat      A     <your-k8s-ingress-ip>
api.mindroom.chat        A     <your-k8s-ingress-ip>
webhooks.mindroom.chat   A     <your-k8s-ingress-ip>

# Wildcard for customer instances
*.mindroom.chat          A     <your-k8s-ingress-ip>
*.api.mindroom.chat      A     <your-k8s-ingress-ip>
*.matrix.mindroom.chat   A     <your-k8s-ingress-ip>
```

### For Local Testing

Add to `/etc/hosts`:

```
127.0.0.1  app.mindroom.chat admin.mindroom.chat api.mindroom.chat webhooks.mindroom.chat
127.0.0.1  demo.mindroom.chat demo.api.mindroom.chat demo.matrix.mindroom.chat
```

Then port-forward the ingress controller:
```bash
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 80:80
```

## SSL/TLS Certificates

### Option 1: cert-manager (Recommended)
Install cert-manager for automatic Let's Encrypt certificates:

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml
```

Then add this annotation to Ingress resources:
```yaml
cert-manager.io/cluster-issuer: letsencrypt-prod
```

### Option 2: Cloudflare (Simple)
Put Cloudflare in front with SSL termination and proxy to your K8s cluster.

## Deploy Examples

### Platform (Staging)
```bash
helm install platform-staging platform/ \
  -f platform/values-staging.yaml \
  --set domain=staging.mindroom.chat
```

### Platform (Production)
```bash
helm install platform-prod platform/ \
  -f platform/values-prod.yaml \
  --set domain=mindroom.chat
```

### Customer Instance
```bash
helm install acme instance/ \
  --set customer=acme \
  --set baseDomain=mindroom.chat \
  --set openai_key=$OPENAI_API_KEY
```

This creates:
- `acme.mindroom.chat`
- `acme.api.mindroom.chat`
- `acme.matrix.mindroom.chat`

## Testing Locally

1. Update `/etc/hosts` as shown above
2. Deploy with kind:
```bash
./kind-setup.sh
helm install platform-staging platform/ -f platform/values-staging.yaml
helm install demo instance/ --set customer=demo --set baseDomain=mindroom.chat
```

3. Port-forward ingress:
```bash
kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80
```

4. Access services:
- http://app.mindroom.chat:8080
- http://admin.mindroom.chat:8080
- http://demo.mindroom.chat:8080
