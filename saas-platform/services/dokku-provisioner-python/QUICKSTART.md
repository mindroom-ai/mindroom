# 🚀 Quick Start Guide - Test MindRoom Dokku Provisioner

## Option 1: Local Testing (No Real Server Needed)

### 1. Setup Local Test Environment
```bash
cd services/dokku-provisioner
./setup_local_test.sh
```

### 2. Start Mock Services
```bash
# Terminal 1: Start mock Supabase
python3 mock_supabase.py

# Terminal 2: Start provisioner
docker-compose up --build
```

### 3. Test the API
```bash
# Terminal 3: Run test suite
python3 test_provisioner.py starter
```

---

## Option 2: Real Hetzner + Dokku Testing (~€13/month)

### 1. Get Hetzner Cloud Server

```bash
# A. Sign up at https://www.hetzner.com/cloud
# B. Get API token from Security → API Tokens

# C. Install Hetzner CLI
brew install hcloud  # macOS

# D. Create server (takes ~30 seconds)
hcloud server create \
  --name dokku-test \
  --type cx22 \
  --image ubuntu-22.04 \
  --location fsn1 \
  --ssh-key ~/.ssh/id_rsa.pub

# Note the IP address!
export SERVER_IP=<YOUR_SERVER_IP>
```

### 2. Install Dokku (5 minutes)

```bash
# SSH to server
ssh root@$SERVER_IP

# Run this one-liner to install Dokku + plugins
curl -sSL https://raw.githubusercontent.com/yourusername/mindroom/main/services/dokku-provisioner/setup/install-dokku.sh | bash

# Or manually:
wget -NP . https://dokku.com/install/v0.34.4/bootstrap.sh
sudo DOKKU_TAG=v0.34.4 bash bootstrap.sh

# Install plugins
dokku plugin:install https://github.com/dokku/dokku-postgres.git postgres
dokku plugin:install https://github.com/dokku/dokku-redis.git redis
dokku plugin:install https://github.com/dokku/dokku-letsencrypt.git
dokku plugin:install https://github.com/dokku/dokku-resource-limit.git resource-limit

# Exit server
exit
```

### 3. Setup Provisioner Locally

```bash
cd services/dokku-provisioner

# Generate SSH key for Dokku
ssh-keygen -t ed25519 -f ssh/dokku_key -N ""

# Add to Dokku server
cat ssh/dokku_key.pub | ssh root@$SERVER_IP "sudo sshcommand acl-add dokku provisioner"

# Configure environment
cat > .env << EOF
DOKKU_HOST=$SERVER_IP
DOKKU_USER=dokku
DOKKU_SSH_KEY_PATH=/app/ssh/dokku_key
DOKKU_PORT=22

# Use nip.io for automatic DNS
BASE_DOMAIN=$SERVER_IP.nip.io

# Test with nginx first
MINDROOM_BACKEND_IMAGE=nginx:alpine
MINDROOM_FRONTEND_IMAGE=nginx:alpine

# Mock Supabase
SUPABASE_URL=http://host.docker.internal:8003
SUPABASE_SERVICE_KEY=mock_key

LOG_LEVEL=DEBUG
EOF
```

### 4. Run Test!

```bash
# Start mock Supabase
python3 mock_supabase.py &

# Start provisioner
docker-compose up --build &

# Wait 5 seconds
sleep 5

# Run test provisioning
python3 test_provisioner.py starter --no-cleanup
```

### 5. Check Your Instance!

```bash
# Get the app name from test output (e.g., mr-test-abc12345)
APP_NAME=mr-test-abc12345  # Replace with actual

# Check on Dokku server
ssh dokku@$SERVER_IP apps:list
ssh dokku@$SERVER_IP apps:report $APP_NAME

# Visit in browser (will show nginx welcome page)
open http://$APP_NAME.$SERVER_IP.nip.io
```

### 6. Cleanup

```bash
# Remove test instance
curl -X DELETE http://localhost:8002/api/v1/deprovision \
  -H "Content-Type: application/json" \
  -d "{\"subscription_id\": \"test_starter_xxx\", \"app_name\": \"$APP_NAME\", \"backup_data\": false}"

# Delete Hetzner server when done testing
hcloud server delete dokku-test
```

---

## Option 3: Quick API Test (No Infrastructure)

Just want to see if the code works?

```bash
cd services/dokku-provisioner

# Install deps
pip3 install -r requirements.txt

# Create minimal .env
echo "DOKKU_HOST=fake.server
DOKKU_USER=dokku
SUPABASE_URL=http://localhost:8003
SUPABASE_SERVICE_KEY=test" > .env

# Create fake SSH key
mkdir -p ssh && touch ssh/dokku_key && chmod 600 ssh/dokku_key

# Run directly
python3 -m uvicorn app.main:app --reload --port 8002

# Test health endpoint
curl http://localhost:8002/health
```

---

## 💰 Cost Breakdown

| Option | Cost | What You Get |
|--------|------|--------------|
| Local Mock | Free | API testing, no real instances |
| Hetzner CX22 | €4.51/month | 2 vCPU, 4GB RAM, hosts ~5 instances |
| Hetzner CPX31 | €13.10/month | 4 vCPU, 8GB RAM, hosts ~15 instances |
| Hetzner CAX11 | €4.51/month | 2 ARM vCPU, 4GB RAM (cheaper ARM) |

---

## 🎯 What to Test

1. **Health Check**: `curl http://localhost:8002/health`
2. **Provision**: Creates instance with chosen tier
3. **Status**: Check instance state
4. **Update**: Change resource limits
5. **Deprovision**: Remove instance and cleanup

---

## 📝 Notes for Real MindRoom Images

When you're ready to use real MindRoom images:

1. Build and push to registry:
```bash
cd /home/basnijholt/Work/mindroom
docker build -f deploy/Dockerfile.backend -t yourdockerhub/mindroom-backend:latest .
docker build -f deploy/Dockerfile.frontend -t yourdockerhub/mindroom-frontend:latest .
docker push yourdockerhub/mindroom-backend:latest
docker push yourdockerhub/mindroom-frontend:latest
```

2. Update .env:
```bash
MINDROOM_BACKEND_IMAGE=yourdockerhub/mindroom-backend:latest
MINDROOM_FRONTEND_IMAGE=yourdockerhub/mindroom-frontend:latest
```

---

## 🆘 Troubleshooting

**SSH Connection Failed**
```bash
ssh -vvv -i ssh/dokku_key dokku@$SERVER_IP version
```

**Check Dokku Logs**
```bash
ssh dokku@$SERVER_IP logs --tail 100
```

**View Provisioner Logs**
```bash
docker-compose logs -f dokku-provisioner
```

**Reset Everything**
```bash
docker-compose down -v
rm -rf ssh/dokku_key* .env
./setup_local_test.sh
```
