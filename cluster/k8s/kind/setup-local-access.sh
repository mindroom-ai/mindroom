#!/usr/bin/env bash
set -euo pipefail

# Setup local access with proper ingress and DNS for kind cluster
echo "🌐 Setting up local access for MindRoom kind cluster"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}✓${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }

# Create ingress for platform with local domain
echo "📝 Creating local ingress for platform..."
cat <<EOF | kubectl apply -f -
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: platform-local
  namespace: mindroom-staging
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "10m"
spec:
  ingressClassName: nginx
  rules:
  - host: platform.local
    http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: platform-backend
            port:
              number: 8000
      - path: /
        pathType: Prefix
        backend:
          service:
            name: platform-frontend
            port:
              number: 3000
EOF

# Create ingress for instance 1
echo "📝 Creating local ingress for instance 1..."
cat <<EOF | kubectl apply -f -
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: instance-1-local
  namespace: mindroom-instances
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "10m"
spec:
  ingressClassName: nginx
  rules:
  - host: instance1.local
    http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: mindroom-backend-1
            port:
              number: 8000
      - path: /
        pathType: Prefix
        backend:
          service:
            name: mindroom-frontend-1
            port:
              number: 8080
EOF

echo ""
echo "📌 Setting up local DNS..."
echo ""
echo "Add these lines to your /etc/hosts file:"
echo "----------------------------------------"
echo "127.0.0.1 platform.local"
echo "127.0.0.1 instance1.local"
echo "----------------------------------------"
echo ""
echo "You can add them with:"
echo "echo '127.0.0.1 platform.local' | sudo tee -a /etc/hosts"
echo "echo '127.0.0.1 instance1.local' | sudo tee -a /etc/hosts"
echo ""

# Port-forward ingress controller to local ports
echo "🔌 Setting up port forwarding..."
echo ""
echo "Run these commands in separate terminals:"
echo ""
echo "# For HTTP access (port 80):"
echo "kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8080:80"
echo ""
echo "# For HTTPS access (port 443):"
echo "kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8443:443"
echo ""

# Check current ingresses
echo "📊 Current ingresses:"
kubectl get ingress -A

echo ""
echo "✅ Setup complete!"
echo ""
echo "After adding hosts entries and starting port-forward, access:"
echo "  - Platform: http://platform.local:8080"
echo "  - Instance 1: http://instance1.local:8080"
echo ""
echo "Direct service access (without ingress):"
echo "  kubectl port-forward -n mindroom-staging svc/platform-frontend 3000:3000"
echo "  kubectl port-forward -n mindroom-staging svc/platform-backend 8000:8000"
