#!/usr/bin/env bash
# Start fresh K8s environment for MindRoom

set -e

echo "🚀 Starting fresh MindRoom K8s setup..."

# nix-shell --run "kind delete cluster --name mindroom"

# 1. Create kind cluster
echo "📦 Creating kind cluster..."
./kind-setup.sh

echo ""
echo "⏳ Waiting for cluster to be ready..."
sleep 10

# 2. Deploy platform services (staging)
echo "🏗️ Deploying platform services..."
helm install platform-staging platform/ -f platform/values-staging.yaml

echo ""
echo "⏳ Waiting for platform pods..."
kubectl wait --for=condition=ready pod -n mindroom-staging --all --timeout=120s || true

# 3. Show status
echo ""
echo "✅ Platform deployed! Status:"
kubectl get pods -n mindroom-staging

echo ""
echo "📝 Next steps:"
echo "1. Deploy a customer instance:"
echo "   helm install demo instance/ --set customer=demo --set domain=demo.mindroom.chat --set openai_key=\$OPENAI_API_KEY"
echo ""
echo "2. Access services:"
echo "   kubectl port-forward -n mindroom-staging svc/customer-portal 3000:3000"
echo "   kubectl port-forward -n mindroom-staging svc/admin-dashboard 8080:80"
echo ""
echo "3. View logs:"
echo "   kubectl logs -n mindroom-staging -l app=customer-portal"
echo ""
echo "4. Clean up:"
echo "   helm uninstall platform-staging"
echo "   kind delete cluster --name mindroom"
