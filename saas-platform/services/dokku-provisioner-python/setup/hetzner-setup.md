# Hetzner + Dokku Setup Guide for MindRoom Provisioner Testing

## 1. Hetzner Cloud Setup

### Get Hetzner Account & API Token
1. Sign up at https://www.hetzner.com/cloud
2. Create a project in Hetzner Cloud Console
3. Go to Security → API Tokens
4. Generate a new token with Read & Write permissions
5. Save the token securely

### Create Server via Hetzner CLI
```bash
# Install Hetzner CLI
brew install hcloud  # macOS
# or
curl -sSL https://github.com/hetznercloud/cli/releases/latest/download/hcloud-linux-amd64.tar.gz | tar xz
sudo mv hcloud /usr/local/bin/

# Configure CLI
hcloud context create mindroom-dokku
# Enter your API token when prompted

# Create server (CPX31: 4 vCPU, 8GB RAM, ~€13/month)
hcloud server create \
  --name dokku-mindroom \
  --type cpx31 \
  --image ubuntu-22.04 \
  --location fsn1 \
  --ssh-key ~/.ssh/id_rsa.pub
```

## 2. Install Dokku on the Server

```bash
# SSH into your new server
ssh root@<SERVER_IP>

# Install Dokku (v0.34.4 or latest)
wget -NP . https://dokku.com/install/v0.34.4/bootstrap.sh
sudo DOKKU_TAG=v0.34.4 bash bootstrap.sh

# Configure Dokku domain
dokku domains:set-global mindroom.app  # or your domain

# Install required plugins
sudo dokku plugin:install https://github.com/dokku/dokku-postgres.git postgres
sudo dokku plugin:install https://github.com/dokku/dokku-redis.git redis
sudo dokku plugin:install https://github.com/dokku/dokku-letsencrypt.git
sudo dokku plugin:install https://github.com/dokku/dokku-resource-limit.git resource-limit

# Configure Let's Encrypt
dokku letsencrypt:set --global email your-email@example.com
dokku letsencrypt:cron-job --add

# Create storage directory
mkdir -p /var/lib/dokku/data/storage
chown -R dokku:dokku /var/lib/dokku/data/storage
```

## 3. Setup SSH Access for Provisioner

```bash
# On the Dokku server, create a dedicated provisioner user
dokku ssh-keys:add provisioner "$(cat ~/.ssh/provisioner_key.pub)"

# Or use the dokku user directly (simpler for testing)
# Just ensure your local SSH key is added:
cat ~/.ssh/id_rsa.pub | ssh root@<SERVER_IP> "sudo sshcommand acl-add dokku provisioner"
```

## 4. Configure DNS (if using real domain)

### Option A: Use Hetzner DNS
```bash
# Create DNS zone
hcloud dns-zone create --name mindroom.app

# Add A records
hcloud dns-record create --zone mindroom.app --name @ --type A --value <SERVER_IP>
hcloud dns-record create --zone mindroom.app --name "*.mindroom.app" --type A --value <SERVER_IP>
```

### Option B: Use Cloudflare (recommended)
1. Add your domain to Cloudflare
2. Create DNS records:
   - A record: `@` → `<SERVER_IP>`
   - A record: `*` → `<SERVER_IP>` (wildcard for subdomains)
3. Set SSL/TLS to "Full" mode

### Option C: Use nip.io for testing (no DNS setup needed)
```bash
# Your apps will be accessible at:
# app-name.<SERVER_IP>.nip.io
# Example: mr-test-abc123.192.168.1.1.nip.io
```

## 5. Build and Push MindRoom Docker Images

```bash
# From the mindroom root directory
cd /home/basnijholt/Work/mindroom

# Build images
docker build -f deploy/Dockerfile.backend -t mindroom/backend:latest .
docker build -f deploy/Dockerfile.frontend -t mindroom/frontend:latest .

# Tag for your registry (using Docker Hub as example)
docker tag mindroom/backend:latest yourdockerhub/mindroom-backend:latest
docker tag mindroom/frontend:latest yourdockerhub/mindroom-frontend:latest

# Push to registry
docker push yourdockerhub/mindroom-backend:latest
docker push yourdockerhub/mindroom-frontend:latest
```

## 6. Prepare the Provisioner Service

### Create SSH key for Dokku access
```bash
# Generate a dedicated key
ssh-keygen -t ed25519 -f ~/.ssh/dokku_provisioner_key -C "provisioner@mindroom"

# Copy public key to Dokku server
ssh-copy-id -i ~/.ssh/dokku_provisioner_key dokku@<SERVER_IP>

# Copy private key to provisioner service
cp ~/.ssh/dokku_provisioner_key services/dokku-provisioner/ssh/dokku_key
chmod 600 services/dokku-provisioner/ssh/dokku_key
```

### Configure environment
```bash
cd services/dokku-provisioner
cp .env.example .env

# Edit .env with your actual values:
cat > .env << EOF
# Dokku SSH Configuration
DOKKU_HOST=<YOUR_SERVER_IP>  # e.g., 95.217.123.45
DOKKU_USER=dokku
DOKKU_SSH_KEY_PATH=/app/ssh/dokku_key
DOKKU_PORT=22

# Domain Configuration
BASE_DOMAIN=<YOUR_DOMAIN_OR_IP>.nip.io  # e.g., 95.217.123.45.nip.io

# Docker Images (your registry)
MINDROOM_BACKEND_IMAGE=yourdockerhub/mindroom-backend:latest
MINDROOM_FRONTEND_IMAGE=yourdockerhub/mindroom-frontend:latest

# Supabase (for testing, can use mock values)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=mock_key_for_testing

# Resource Defaults
DEFAULT_MEMORY_LIMIT=512m
DEFAULT_CPU_LIMIT=0.5

# Storage
INSTANCE_DATA_BASE=/var/lib/dokku/data/storage

# Logging
LOG_LEVEL=DEBUG
EOF
```

## 7. Test the Provisioner Locally First

```bash
# Start the provisioner service
docker-compose up --build

# In another terminal, test the health endpoint
curl http://localhost:8002/health

# Test SSH connection
curl http://localhost:8002/api/v1/test-connection
```

## 8. Test Provisioning an Instance

```bash
# Create a test provision request
curl -X POST http://localhost:8002/api/v1/provision \
  -H "Content-Type: application/json" \
  -d '{
    "subscription_id": "test_sub_001",
    "account_id": "test_account_001",
    "tier": "starter",
    "limits": {
      "memory_mb": 512,
      "cpu_limit": 0.5,
      "storage_gb": 5,
      "agents": 3,
      "messages_per_day": 500
    },
    "enable_matrix": false
  }'

# Check the instance status
curl http://localhost:8002/api/v1/status/test_sub_001

# Verify on Dokku server
ssh dokku@<SERVER_IP> apps:list
ssh dokku@<SERVER_IP> apps:report mr-test*
```

## 9. Access Your Provisioned Instance

```bash
# Get the URLs from the provision response
# Frontend: https://mr-testXXXX-YYYY.<YOUR_DOMAIN>
# Backend: https://api.mr-testXXXX-YYYY.<YOUR_DOMAIN>

# Test the instance
curl https://mr-testXXXX-YYYY.<YOUR_DOMAIN>
```

## 10. Test Deprovisioning

```bash
curl -X DELETE http://localhost:8002/api/v1/deprovision \
  -H "Content-Type: application/json" \
  -d '{
    "subscription_id": "test_sub_001",
    "app_name": "mr-test-XXXXX",
    "backup_data": false
  }'
```

## Cost Breakdown

- **Hetzner CPX31**: ~€13/month (4 vCPU, 8GB RAM, 160GB SSD)
- **Can host**: ~10-20 starter instances or 5-10 professional instances
- **Domain**: Free with nip.io or ~€10/year for .app domain
- **SSL**: Free with Let's Encrypt

## Monitoring Commands

```bash
# Check server resources
ssh root@<SERVER_IP> htop

# Check Dokku apps
ssh dokku@<SERVER_IP> apps:list

# Check specific app
ssh dokku@<SERVER_IP> apps:report <app-name>

# View logs
ssh dokku@<SERVER_IP> logs <app-name> -t

# Check PostgreSQL databases
ssh dokku@<SERVER_IP> postgres:list

# Check Redis instances
ssh dokku@<SERVER_IP> redis:list
```

## Troubleshooting

### SSH Connection Issues
```bash
# Test SSH connection
ssh -i ssh/dokku_key dokku@<SERVER_IP> version

# Debug SSH
ssh -vvv -i ssh/dokku_key dokku@<SERVER_IP> version
```

### Domain/SSL Issues
```bash
# Check DNS propagation
dig mr-test-xxx.<YOUR_DOMAIN>

# Force SSL renewal
ssh dokku@<SERVER_IP> letsencrypt:enable <app-name>
```

### Resource Issues
```bash
# Check disk space
ssh root@<SERVER_IP> df -h

# Check memory
ssh root@<SERVER_IP> free -m

# Cleanup unused Docker resources
ssh root@<SERVER_IP> docker system prune -a
```
