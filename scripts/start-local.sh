#!/bin/bash
set -e

echo "🚀 Starting MindRoom Platform (Local Development)"

# Start Supabase
echo "Starting Supabase..."
cd supabase
npx supabase start
cd ..

# Start platform services
echo "Starting platform services..."
docker-compose -f deploy/platform/docker-compose.local.yml up -d

# Wait for services
echo "Waiting for services to be ready..."
sleep 10

# Show status
docker-compose -f deploy/platform/docker-compose.local.yml ps

echo "✅ Platform is running!"
echo ""
echo "Access points:"
echo "  - Customer Portal: http://localhost:3000"
echo "  - Admin Dashboard: http://localhost:3001"
echo "  - Stripe Webhooks: http://localhost:3005"
echo "  - Dokku Provisioner: http://localhost:8002"
echo "  - Supabase Studio: http://localhost:54323"
