---
icon: lucide/ship
---

# Kubernetes Deployment

Deploy MindRoom on Kubernetes for production multi-tenant deployments.

## Helm Chart

MindRoom provides Helm charts for Kubernetes deployment.

### Installation

```bash
helm repo add mindroom https://mindroom-ai.github.io/charts
helm install mindroom mindroom/mindroom -f values.yaml
```

### Basic values.yaml

```yaml
replicaCount: 1

image:
  repository: ghcr.io/mindroom-ai/mindroom
  tag: latest

config:
  configMapName: mindroom-config

secrets:
  matrixAccessToken: your-token
  anthropicApiKey: your-key

persistence:
  enabled: true
  size: 10Gi

ingress:
  enabled: true
  hosts:
    - host: mindroom.example.com
      paths:
        - path: /
          pathType: Prefix
```

## ConfigMap

Create a ConfigMap for your config.yaml:

```bash
kubectl create configmap mindroom-config \
  --from-file=config.yaml=./config.yaml
```

## Secrets

Create secrets for sensitive values:

```bash
kubectl create secret generic mindroom-secrets \
  --from-literal=MATRIX_ACCESS_TOKEN=your-token \
  --from-literal=ANTHROPIC_API_KEY=your-key
```

## Multi-Tenant Deployment

For SaaS deployments, use the instance chart:

```bash
helm install instance-1 mindroom/mindroom-instance \
  -f values-instance-1.yaml \
  --namespace mindroom-instances
```

Each instance gets:
- Separate deployment
- Isolated storage
- Independent configuration
