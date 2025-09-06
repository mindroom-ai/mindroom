#!/bin/bash

# Run migrations via SSH tunnel through your platform server
set -e
source .env

echo "🚀 Running migrations via SSH tunnel..."

# Your platform server has internet access
PLATFORM_HOST="159.69.220.57"

# Copy migration file to platform server
echo "Copying migration file to platform server..."
scp -o StrictHostKeyChecking=no supabase/all-migrations.sql root@${PLATFORM_HOST}:/tmp/

# Run migrations from the platform server
echo "Executing migrations from platform server..."
ssh -o StrictHostKeyChecking=no root@${PLATFORM_HOST} << EOF
export PGPASSWORD="${SUPABASE_DB_PASSWORD}"
psql "postgresql://postgres@db.lxcziijbiqaxoavavrco.supabase.co:5432/postgres" -f /tmp/all-migrations.sql
rm /tmp/all-migrations.sql
EOF

echo "✅ Migrations complete!"

# Verify
echo "Verifying tables..."
curl -s "${SUPABASE_URL}/rest/v1/accounts?limit=1" \
    -H "apikey: ${SUPABASE_SERVICE_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" | jq .
